/**
 * 运行统计看板（M5c）：只读，不做图表库——数字卡 + 一张表格足够回答
 * "这批任务表现如何"。数据来自 GET /api/v1/stats，口径说明见 repo.fetch_stats。
 *
 * 为什么手动刷新而不是自动轮询：统计是"跑完评测后回来看"的场景，
 * 自动轮询只会一直空打 DB；挂载时拉一次 + 看板右上角手动刷新就够了。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchStats } from "../api";
import { STATUS_TEXT, statusTone } from "../types";
import type { StatsData, TaskStatus } from "../types";

export default function StatsPanel() {
  const [stats, setStats] = useState<StatsData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setStats(await fetchStats());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="panel stats">
      <div className="stats-head">
        <h2>运行统计</h2>
        <div className="stats-tools">
          {stats?.available && (
            <span className="stats-freshness">
              共 {stats.total} 个任务{stats.unfinished > 0 ? `（${stats.unfinished} 个进行中，未计入）` : ""}
            </span>
          )}
          <button className="stats-refresh" onClick={load} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      {/* 后端降级（DB 不可用）：显示提示而不是报错——统计是增强功能 */}
      {stats && !stats.available && <p className="hint">{stats.note}</p>}

      {stats?.available && (
        <>
          <div className="stat-grid">
            <Stat label="成功率" value={`${stats.success_rate}%`} sub={`${stats.success}/${stats.total}`} />
            <Stat label="平均步数" value={String(stats.avg_steps)} />
            <Stat label="耗时 P50" value={`${stats.elapsed_p50_s}s`} />
            <Stat label="耗时 P95" value={`${stats.elapsed_p95_s}s`} />
            <Stat
              label="Token 总量"
              value={stats.tokens.total.toLocaleString()}
              sub={`输入 ${stats.tokens.prompt.toLocaleString()} / 输出 ${stats.tokens.completion.toLocaleString()}`}
            />
            <Stat
              label="失败任务"
              value={String(stats.total - stats.success)}
              sub={
                Object.entries(stats.status_distribution)
                  .filter(([s]) => s !== "success")
                  .map(([s, n]) => `${STATUS_TEXT[s as TaskStatus] ?? s} ${n}`)
                  .join("、") || "无"
              }
            />
          </div>

          {/* 分任务表：一行一道题（按 goal 聚合），与 eval_real 报告的"分任务"表同构 */}
          {stats.by_goal.length > 0 && (
            <table className="stat-table">
              <thead>
                <tr>
                  <th>任务目标</th>
                  <th>尝试</th>
                  <th>通过</th>
                  <th>成功率</th>
                  <th>平均步数</th>
                  <th>平均耗时</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_goal.map((row) => (
                  <tr key={row.goal}>
                    <td className="stat-goal" title={row.goal}>
                      {row.goal}
                    </td>
                    <td>{row.total}</td>
                    <td>{row.success}</td>
                    <td className={row.success_rate >= 100 ? "good" : row.success_rate === 0 ? "bad" : ""}>
                      {row.success_rate}%
                    </td>
                    <td>{row.avg_steps}</td>
                    <td>{row.avg_elapsed_s}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* 状态分布：pill 一排，复用现有的状态色档 */}
          <div className="stat-dist">
            {Object.entries(stats.status_distribution).map(([status, count]) => (
              <span key={status} className={`pill ${statusTone(status as TaskStatus)}`}>
                {STATUS_TEXT[status as TaskStatus] ?? status} · {count}
              </span>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

/** 单个数字卡。sub 放次要信息（分数、token 细分），没有就不渲染。 */
function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}
