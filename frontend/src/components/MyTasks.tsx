/**
 * 我的任务列表（M4 收尾）。
 *
 * 它在项目里的角色：让"用户隔离"从一句承诺变成看得见的事实。
 * 换个账号登录，这里就是空的——不用读代码、不用 curl，肉眼可验证。
 *
 * 为什么做成折叠（<details>）而不是常驻列表：
 * 左列已经有"提交任务"和"知识库问答"两个主要操作，再来一张常驻大卡片
 * 会把首屏挤满。折叠态只占一行（"我的任务 (3)"），想看再展开。
 * 用原生 <details> 而不是自己写 state：无 JS 也能开合，少一份状态要管。
 *
 * 为什么不自动轮询：任务是"提交完等下一条"的低频操作，
 * 自动轮询只会一直空打 DB。挂载时拉一次 + 右上角刷新 + 任务结束自动刷（见 App.tsx）。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchMyTasks } from "../api";
import { STATUS_TEXT, statusTone } from "../types";
import type { MyTasksResponse, TaskStatus } from "../types";

interface Props {
  /** 点一条任务 → 右列加载它的结论与轨迹 */
  onOpen: (taskId: string) => void;
  /** 外部（提交任务结束后）传进来的刷新信号：值变了就重新拉一次 */
  refreshKey?: number;
}

export default function MyTasks({ onOpen, refreshKey = 0 }: Props) {
  const [data, setData] = useState<MyTasksResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await fetchMyTasks(10));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  // refreshKey 变化 = 外部要求刷新（提交新任务后）。
  // 这里把 load 的依赖拆开写：如果依赖 load，每次 render 都会重新拉一遍。
  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const items = data?.items ?? [];
  const count = data?.total ?? 0;

  return (
    <details className="panel my-tasks" open>
      <summary>
        <span className="my-tasks-title">
          我的任务 <span className="badge">{count}</span>
        </span>
        <button
          type="button"
          className="link-btn"
          onClick={(e) => {
            // summary 里的按钮默认会连带着开关 <details>，拦掉
            e.preventDefault();
            e.stopPropagation();
            load();
          }}
          disabled={loading}
        >
          {loading ? "刷新中…" : "刷新"}
        </button>
      </summary>

      {error && <p className="error">{error}</p>}
      {/* 后端降级（DB 不可用）：提示而不是报错，与统计看板同口径 */}
      {data && !data.available && <p className="hint">{data.note ?? "任务列表暂不可用"}</p>}

      {data?.available && items.length === 0 && (
        <p className="hint">这个账号还没有任务。提交一个试试。</p>
      )}

      {items.length > 0 && (
        <ul className="my-tasks-list">
          {items.map((t) => (
            <li key={t.task_id}>
              <button
                type="button"
                className="my-task-item"
                onClick={() => onOpen(t.task_id)}
                title={`${t.goal}\n${t.task_id}`}
              >
                {/* 复用 ResultPanel 的状态胶囊，同一状态在两个地方颜色一致 */}
                <span className={`pill ${statusTone(t.status as TaskStatus)}`}>
                  {STATUS_TEXT[t.status as TaskStatus] ?? t.status}
                </span>
                <span className="my-task-goal">{t.goal}</span>
                <span className="my-task-meta">{t.steps} 步</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
