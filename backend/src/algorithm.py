import hashlib, json
import numpy as np

ALGORITHM_STL = 'stl_rolling'
ALGORITHM_RECENT_FOUR = 'recent_four_mean'
ALGORITHM_LABELS = {
    ALGORITHM_STL: 'STL 季节分解 + 滚动线性回归（旧版）',
    ALGORITHM_RECENT_FOUR: '最近四笔计数均值（候选版）',
}
DEFAULT_ALGORITHM = ALGORITHM_STL


def stl_rolling_forecast(points, window_size=8):
    points = sorted(points, key=lambda row: row['counted_at'])
    values = np.array([float(row['bag_count']) for row in points], dtype=float)
    if len(values) < max(window_size, 8): raise ValueError('至少需要 8 个连续计数点')
    period = 4 if len(values) < 16 else 8
    seasonal_template = np.array([values[i::period].mean() for i in range(period)])
    seasonal = np.array([seasonal_template[i % period] for i in range(len(values))])
    deseasonal = values - seasonal + values.mean()
    kernel = np.ones(min(5, len(values))) / min(5, len(values))
    trend = np.convolve(deseasonal, kernel, mode='same')
    trend[:2] = deseasonal[:2]
    trend[-2:] = deseasonal[-2:]
    residual = values - (trend + seasonal - values.mean())
    recent = deseasonal[-window_size:]
    x = np.arange(window_size, dtype=float)
    slope, intercept = np.polyfit(x, recent, 1)
    future_x = np.arange(window_size, window_size + 4, dtype=float)
    future_trend = intercept + slope * future_x
    future_seasonal = np.array([seasonal_template[(len(values)+i) % period] for i in range(4)])
    predicted = np.maximum(0, future_trend + future_seasonal - values.mean())
    peak = float(predicted.max())
    strength = float(max(0, 1 - np.var(residual) / max(np.var(values), 1)))
    snapshot = hashlib.sha256(json.dumps([(str(p['counted_at']), p['bag_count']) for p in points], default=str).encode()).hexdigest()
    return {'peak_bags': round(peak, 2), 'seasonal_strength': round(strength, 4), 'trend_per_hour': round(float(slope), 4), 'window_size': window_size, 'period': period, 'predicted_next_4': [round(float(v),2) for v in predicted], 'snapshot_hash': snapshot, 'input_points': len(values)}


def recent_four_mean_forecast(points, window_size=4):
    """候选版峰值算法：取最近四笔已接受计数的均值作为预测峰值。

    与旧版一样对同一批按时间排序的计数点计算，并产出相同结构的结果，
    便于两版在指定波次上并行试算与比对。
    """
    points = sorted(points, key=lambda row: row['counted_at'])
    values = np.array([float(row['bag_count']) for row in points], dtype=float)
    if len(values) < 4:
        raise ValueError('候选版至少需要 4 个连续计数点')
    window = min(4, len(values))
    recent = values[-window:]
    peak = float(recent.mean())
    # 候选口径不做季节分解：未来 4 点以同一均值水平给出，趋势/季节强度置 0
    predicted = np.repeat(peak, 4)
    snapshot = hashlib.sha256(json.dumps([(str(p['counted_at']), p['bag_count']) for p in points], default=str).encode()).hexdigest()
    return {
        'peak_bags': round(peak, 2),
        'seasonal_strength': 0.0,
        'trend_per_hour': 0.0,
        'window_size': window,
        'period': 1,
        'predicted_next_4': [round(float(v), 2) for v in predicted],
        'snapshot_hash': snapshot,
        'input_points': len(values),
        'recent_counts': [int(v) for v in recent],
    }


FORECAST_FUNCS = {
    ALGORITHM_STL: stl_rolling_forecast,
    ALGORITHM_RECENT_FOUR: recent_four_mean_forecast,
}


def run_forecast(algorithm_version, points, window_size=None):
    """按算法版本分派预测，返回与版本无关的统一结果结构。"""
    if algorithm_version not in FORECAST_FUNCS:
        raise ValueError(f'未知算法版本: {algorithm_version}')
    if algorithm_version == ALGORITHM_RECENT_FOUR:
        return recent_four_mean_forecast(points, window_size or 4)
    return stl_rolling_forecast(points, window_size or 8)
