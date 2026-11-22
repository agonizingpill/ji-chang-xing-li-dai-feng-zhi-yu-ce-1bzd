import json, os, time
from contextlib import asynccontextmanager
from datetime import datetime
import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .casework_routes import router as operational_cases
from .operations_evidence import router as operations_evidence
from .auth import current_user, issue, password_hash, require
from .db import audit, connect, migrate, wait_for_db
from .algorithm import ALGORITHM_LABELS, ALGORITHM_RECENT_FOUR, ALGORITHM_STL
from .schemas import Login, WaveIn, CountIn, ForecastIn, TrialIn, ShiftIn, ConfirmIn, ReviewIn

r=redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)

def dump(rows): return [dict(row) for row in rows]

def effective_algorithm(conn):
    return conn.execute('SELECT effective_version FROM algorithm_settings WHERE id=1').fetchone()['effective_version']

def forecast_brief(row):
    if not row: return None
    return {k: row[k] for k in ('id','status','peak_bags','failure_reason','snapshot_hash','algorithm_version','created_at','updated_at')}

def algorithm_overview(conn):
    effective = effective_algorithm(conn)
    trials = []
    for t in conn.execute('SELECT * FROM algorithm_trials ORDER BY designated_at').fetchall():
        wave = conn.execute('SELECT code,terminal FROM flight_waves WHERE id=%s',(t['wave_id'],)).fetchone()
        stl = forecast_brief(conn.execute('SELECT * FROM peak_forecasts WHERE id=%s',(t['stl_forecast_id'],)).fetchone()) if t['stl_forecast_id'] else None
        recent = forecast_brief(conn.execute('SELECT * FROM peak_forecasts WHERE id=%s',(t['recent_four_forecast_id'],)).fetchone()) if t['recent_four_forecast_id'] else None
        reason = None
        if not stl or not recent:
            reason = '试算任务尚未建立完整'
        elif stl['status'] == 'failed' or recent['status'] == 'failed':
            reason = '存在失败的试算，保留旧版，可重新发起试算'
        elif stl['status'] != 'ready' or recent['status'] != 'ready':
            reason = '两版试算尚未全部完成'
        elif stl['snapshot_hash'] != recent['snapshot_hash']:
            reason = '两版输入计数批次不一致，需重新试算'
        trials.append({'trial_id': t['id'], 'wave_id': t['wave_id'], 'code': wave['code'], 'terminal': wave['terminal'],
                       'designated_at': t['designated_at'], 'stl_trial': stl, 'recent_four_trial': recent,
                       'ready_for_switch': reason is None, 'blocking_reason': reason})
    return {'effective_version': effective, 'effective_label': ALGORITHM_LABELS[effective],
            'candidate_version': ALGORITHM_RECENT_FOUR, 'candidate_label': ALGORITHM_LABELS[ALGORITHM_RECENT_FOUR],
            'legacy_version': ALGORITHM_STL, 'trials': trials,
            'switch_possible': bool(trials) and all(item['ready_for_switch'] for item in trials)}
@asynccontextmanager
async def lifespan(_):
    wait_for_db(); migrate()
    if os.getenv('SEED_DEMO','true').lower()=='true':
        from scripts.seed_demo import main as seed_main
        seed_main()
    yield
app=FastAPI(title='机场行李带峰值预测', lifespan=lifespan)
app.include_router(operational_cases)
app.include_router(operations_evidence)
@app.middleware('http')
async def guard(request: Request, call_next):
    started=time.monotonic()
    try: response=await call_next(request)
    except HTTPException: raise
    except Exception as exc: return JSONResponse(status_code=500,content={'detail':str(exc)})
    response.headers['X-Request-ID']=os.urandom(8).hex(); response.headers['X-Process-Time']=f'{time.monotonic()-started:.4f}'; response.headers['Cache-Control']='no-store'; return response
@app.get('/health')
def health():
    with connect() as conn: conn.execute('SELECT 1')
    return {'status':'healthy','redis':bool(r.ping())}
@app.post('/login')
def login(data: Login):
    with connect() as conn:
        user=conn.execute('SELECT id,username,display_name,role,password_hash FROM users WHERE username=%s AND active',(data.username,)).fetchone()
        if not user or user['password_hash']!=password_hash(data.password): raise HTTPException(401,'用户名或密码不正确')
        audit(conn,user['id'],'login','user',user['id'],{'role':user['role']}); conn.commit()
        return {'access_token':issue(user),'token_type':'bearer','user':{k:user[k] for k in ('id','username','display_name','role')}}
@app.get('/me')
def me(user=Depends(current_user)): return user
@app.get('/dashboard')
def dashboard(user=Depends(current_user)):
    with connect() as conn:
        return {'waves':conn.execute('SELECT COUNT(*) c FROM flight_waves').fetchone()['c'],'active_waves':conn.execute("SELECT COUNT(*) c FROM flight_waves WHERE status IN ('open','counting','forecasted')").fetchone()['c'],'forecasts_ready':conn.execute("SELECT COUNT(*) c FROM peak_forecasts WHERE status IN ('ready','published') AND purpose='operational'").fetchone()['c'],'blocked_waves':conn.execute("SELECT COUNT(*) c FROM peak_forecasts WHERE status='blocked' AND purpose='operational'").fetchone()['c'],'needs_confirmation':conn.execute("SELECT COUNT(*) c FROM staff_shifts WHERE status='needs_confirmation'").fetchone()['c'],'open_reviews':conn.execute("SELECT COUNT(*) c FROM congestion_reviews WHERE status!='closed'").fetchone()['c']}
@app.get('/waves')
def waves(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT * FROM flight_waves ORDER BY arrival_start').fetchall())
@app.post('/waves',status_code=201)
def create_wave(data: WaveIn,user=Depends(require('operator','supervisor'))):
    if data.arrival_end<=data.arrival_start: raise HTTPException(400,'到达结束时间必须晚于开始时间')
    with connect() as conn:
        try:
            row=conn.execute('INSERT INTO flight_waves(code,terminal,arrival_start,arrival_end,flight_count,planned_bags,cancellation_state,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,\'open\') RETURNING *',(data.code,data.terminal,data.arrival_start,data.arrival_end,data.flight_count,data.planned_bags,data.cancellation_state,data.notes)).fetchone()
        except Exception as exc: raise HTTPException(409,'航班波次编号已存在或字段不符合约束') from exc
        audit(conn,user['id'],'create','flight_wave',row['id'],{'code':row['code']}); conn.commit(); return row
@app.get('/waves/{wave_id}/counts')
def counts(wave_id:int,user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT * FROM belt_counts WHERE wave_id=%s ORDER BY counted_at',(wave_id,)).fetchall())
@app.post('/counts',status_code=201)
def create_count(data:CountIn,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        wave=conn.execute('SELECT * FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'航班波次不存在')
        try: row=conn.execute('INSERT INTO belt_counts(wave_id,belt_code,counted_at,bag_count,scan_source) VALUES (%s,%s,%s,%s,%s) RETURNING *',(data.wave_id,data.belt_code,data.counted_at,data.bag_count,data.scan_source)).fetchone()
        except Exception as exc: raise HTTPException(409,'同一航班波次、行李带和时间不能重复计数') from exc
        conn.execute("UPDATE flight_waves SET status='counting' WHERE id=%s AND status='open'",(data.wave_id,)); audit(conn,user['id'],'create','belt_count',row['id'],{'wave_id':data.wave_id}); conn.commit(); return row
@app.get('/forecasts')
def forecasts(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute("SELECT f.*,w.code,w.terminal FROM peak_forecasts f JOIN flight_waves w ON w.id=f.wave_id WHERE f.purpose='operational' ORDER BY f.id DESC").fetchall())
@app.get('/algorithm/overview')
def get_algorithm_overview(user=Depends(current_user)):
    with connect() as conn: return algorithm_overview(conn)
@app.post('/forecasts',status_code=202)
def create_forecast(data:ForecastIn,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        wave=conn.execute('SELECT * FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'航班波次不存在')
        if wave['cancellation_state']=='cancelled_unmarked':
            raise HTTPException(409,'航班取消或改降尚未标记，禁止发布峰值预测')
        version=effective_algorithm(conn)
        point_count=conn.execute("SELECT COUNT(*) c FROM belt_counts WHERE wave_id=%s AND status='accepted'",(data.wave_id,)).fetchone()['c']
        min_points = 4 if version==ALGORITHM_RECENT_FOUR else 8
        if point_count<min_points: raise HTTPException(409,f'当前生效算法（{ALGORITHM_LABELS[version]}）至少需要 {min_points} 个计数点')
        row=conn.execute("INSERT INTO peak_forecasts(wave_id,status,window_size,created_by,algorithm_version,purpose) VALUES (%s,'queued',%s,%s,%s,'operational') RETURNING *",(data.wave_id,data.window_size,user['id'],version)).fetchone(); audit(conn,user['id'],'queue','peak_forecast',row['id'],{'method':ALGORITHM_LABELS[version],'algorithm_version':version}); conn.commit()
    try: r.rpush('airport:forecast',str(row['id']))
    except Exception: pass
    return row
@app.post('/algorithm/trials',status_code=202)
def designate_trial(data:TrialIn,user=Depends(require('supervisor'))):
    """指定波次用同一批已接受计数同时试算旧版与候选版。试算期间排班照旧。"""
    with connect() as conn:
        wave=conn.execute('SELECT * FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'航班波次不存在')
        point_count=conn.execute("SELECT COUNT(*) c FROM belt_counts WHERE wave_id=%s AND status='accepted'",(data.wave_id,)).fetchone()['c']
        if point_count<8: raise HTTPException(409,'计数点不足 8 个，无法对两版算法使用同一批计数完成试算')
        existing=conn.execute('SELECT * FROM algorithm_trials WHERE wave_id=%s',(data.wave_id,)).fetchone()
        forecast_ids=[]
        if existing:
            # 重新试算：复用原任务行并清空旧结果，保证两版仍取同一批计数
            for column, version, window in (('stl_forecast_id',ALGORITHM_STL,8),('recent_four_forecast_id',ALGORITHM_RECENT_FOUR,4)):
                fid=existing[column]
                if fid:
                    conn.execute("UPDATE peak_forecasts SET status='queued',failure_reason=NULL,updated_at=now() WHERE id=%s",(fid,))
                else:
                    fid=conn.execute("INSERT INTO peak_forecasts(wave_id,status,window_size,created_by,algorithm_version,purpose) VALUES (%s,'queued',%s,%s,%s,'trial') RETURNING id",(data.wave_id,window,user['id'],version)).fetchone()['id']
                    conn.execute(f'UPDATE algorithm_trials SET {column}=%s WHERE id=%s',(fid,existing['id']))
                forecast_ids.append(fid)
            conn.execute('UPDATE algorithm_trials SET designated_by=%s,designated_at=now() WHERE id=%s',(user['id'],existing['id']))
            trial_id=existing['id']
        else:
            stl_row=conn.execute("INSERT INTO peak_forecasts(wave_id,status,window_size,created_by,algorithm_version,purpose) VALUES (%s,'queued',8,%s,%s,'trial') RETURNING id",(data.wave_id,user['id'],ALGORITHM_STL)).fetchone()
            recent_row=conn.execute("INSERT INTO peak_forecasts(wave_id,status,window_size,created_by,algorithm_version,purpose) VALUES (%s,'queued',4,%s,%s,'trial') RETURNING id",(data.wave_id,user['id'],ALGORITHM_RECENT_FOUR)).fetchone()
            trial=conn.execute('INSERT INTO algorithm_trials(wave_id,designated_by,stl_forecast_id,recent_four_forecast_id) VALUES (%s,%s,%s,%s) RETURNING id',(data.wave_id,user['id'],stl_row['id'],recent_row['id'])).fetchone()
            forecast_ids=[stl_row['id'],recent_row['id']]; trial_id=trial['id']
        audit(conn,user['id'],'designate_trial','algorithm_trial',trial_id,{'wave_id':data.wave_id,'forecast_ids':forecast_ids}); conn.commit()
    for fid in forecast_ids:
        try: r.rpush('airport:forecast',str(fid))
        except Exception: pass
    with connect() as conn: return algorithm_overview(conn)
@app.post('/algorithm/switch')
def switch_algorithm(user=Depends(require('supervisor'))):
    """两版试算全部成功后才能一次性切换到候选版；任一失败或未完成则保留旧版。"""
    with connect() as conn:
        settings=conn.execute('SELECT * FROM algorithm_settings WHERE id=1 FOR UPDATE').fetchone()
        if settings['effective_version']==ALGORITHM_RECENT_FOUR:
            raise HTTPException(409,'候选版已经是当前生效版本')
        overview=algorithm_overview(conn)
        if not overview['trials']:
            raise HTTPException(409,'尚未指定任何试算波次，不能切换峰值算法')
        if not overview['switch_possible']:
            blocked=[f"{t['code']}：{t['blocking_reason']}" for t in overview['trials'] if not t['ready_for_switch']]
            raise HTTPException(409,'存在未通过的两版试算，保留旧版。'+'；'.join(blocked))
        conn.execute('UPDATE algorithm_settings SET effective_version=%s,switched_by=%s,switched_at=now() WHERE id=1',(ALGORITHM_RECENT_FOUR,user['id']))
        audit(conn,user['id'],'switch_algorithm','algorithm_settings',1,{'from':ALGORITHM_STL,'to':ALGORITHM_RECENT_FOUR,'trial_waves':[t['code'] for t in overview['trials']]}); conn.commit()
        return algorithm_overview(conn)
@app.post('/forecasts/{forecast_id}/publish')
def publish(forecast_id:int,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        f=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s',(forecast_id,)).fetchone()
        if not f: raise HTTPException(404,'预测不存在')
        if f['purpose']!='operational': raise HTTPException(409,'试算预测不能发布到排班')
        if f['status']=='published': return f  # 已发布排班永久保留原峰值，重复发布幂等返回
        if f['status']!='ready': raise HTTPException(409,'预测尚未就绪')
        # 锁住生效版本行，与换版事务互斥，确保检查与发布之间版本不会被切换
        settings=conn.execute('SELECT * FROM algorithm_settings WHERE id=1 FOR UPDATE').fetchone()
        if f['algorithm_version']!=settings['effective_version']:
            raise HTTPException(409,f"该预测使用 {ALGORITHM_LABELS[f['algorithm_version']]} 生成，当前生效版本为 {ALGORITHM_LABELS[settings['effective_version']]}，只能发布生效版本的预测")
        shift=conn.execute('SELECT * FROM staff_shifts WHERE wave_id=%s ORDER BY id DESC LIMIT 1',(f['wave_id'],)).fetchone()
        if not shift: raise HTTPException(409,'发布前必须先建立人员班次')
        if float(f['peak_bags'])>shift['capacity']*10 and shift['status']!='confirmed':
            conn.execute("UPDATE staff_shifts SET status='needs_confirmation' WHERE id=%s",(shift['id'],)); conn.commit(); raise HTTPException(409,'预测峰值超过当前岗位容量，需拆分班次并由值班经理确认')
        row=conn.execute("UPDATE peak_forecasts SET status='published',updated_at=now() WHERE id=%s RETURNING *",(forecast_id,)).fetchone(); audit(conn,user['id'],'publish','peak_forecast',forecast_id,{'algorithm_version':f['algorithm_version'],'peak_bags':float(f['peak_bags'])}); conn.commit(); return row
@app.post('/forecasts/{forecast_id}/retry',status_code=202)
def retry(forecast_id:int,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        row=conn.execute("UPDATE peak_forecasts SET status='queued',failure_reason=NULL WHERE id=%s AND status='failed' RETURNING *",(forecast_id,)).fetchone()
        if not row: raise HTTPException(409,'只有失败预测可以重试')
        conn.commit()
    r.rpush('airport:forecast',str(forecast_id)); return row
@app.get('/shifts')
def shifts(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT s.*,w.code FROM staff_shifts s JOIN flight_waves w ON w.id=s.wave_id ORDER BY s.start_at').fetchall())
@app.post('/shifts',status_code=201)
def create_shift(data:ShiftIn,user=Depends(require('operator','supervisor'))):
    if data.end_at<=data.start_at: raise HTTPException(400,'班次结束时间必须晚于开始时间')
    with connect() as conn:
        wave=conn.execute('SELECT id FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'波次不存在')
        status='needs_confirmation' if data.planned_staff>data.capacity else 'draft'
        row=conn.execute('INSERT INTO staff_shifts(shift_code,wave_id,start_at,end_at,planned_staff,capacity,status) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *',(data.shift_code,data.wave_id,data.start_at,data.end_at,data.planned_staff,data.capacity,status)).fetchone(); audit(conn,user['id'],'create','staff_shift',row['id'],{'status':status}); conn.commit(); return row
@app.post('/shifts/{shift_id}/confirm')
def confirm_shift(shift_id:int,data:ConfirmIn,user=Depends(require('supervisor'))):
    with connect() as conn:
        row=conn.execute("UPDATE staff_shifts SET status='confirmed',confirmation_note=%s,confirmed_by=%s,confirmed_at=now() WHERE id=%s RETURNING *",(data.note,user['id'],shift_id)).fetchone()
        if not row: raise HTTPException(404,'班次不存在')
        audit(conn,user['id'],'confirm','staff_shift',shift_id,{'note':data.note}); conn.commit(); return row
@app.get('/reviews')
def reviews(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT r.*,w.code FROM congestion_reviews r JOIN flight_waves w ON w.id=r.wave_id ORDER BY r.id DESC').fetchall())
@app.post('/reviews',status_code=201)
def create_review(data:ReviewIn,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        wave=conn.execute('SELECT id FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'波次不存在')
        row=conn.execute('INSERT INTO congestion_reviews(wave_id,forecast_id,observed_peak,queue_minutes,cause,action) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *',(data.wave_id,data.forecast_id,data.observed_peak,data.queue_minutes,data.cause,data.action)).fetchone(); audit(conn,user['id'],'create','congestion_review',row['id'],{}); conn.commit(); return row
@app.post('/reviews/{review_id}/close')
def close_review(review_id:int,user=Depends(require('supervisor'))):
    with connect() as conn:
        row=conn.execute("UPDATE congestion_reviews SET status='closed',reviewed_by=%s,reviewed_at=now() WHERE id=%s RETURNING *",(user['id'],review_id)).fetchone()
        if not row: raise HTTPException(404,'复盘单不存在')
        audit(conn,user['id'],'close','congestion_review',review_id,{}); conn.commit(); return row
