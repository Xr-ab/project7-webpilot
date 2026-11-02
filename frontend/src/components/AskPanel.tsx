/**
 * 知识库问答入口（P4-N4 的前端）。
 *
 * 它解决什么：Agent 跑过的任务会沉淀成知识（P4-N1/N2），这个面板让用户能
 * "问一句、得到带来源的答案"——从"跑任务"变成"用积累的数据"。
 *
 * 设计上的两个刻意选择：
 * 1. **来源必须展示**：答案里的 [n] 引用要能对应到下面对应的来源条目。
 *    不给来源的问答系统无法被信任——用户没法验证答案是查到的还是编的。
 * 2. **不加自动轮询/自动问答**：每次提问都要跑一次检索 + 一次 LLM 生成（约 2~4 秒），
 *    不是"输入时实时联想"的场景，点按钮/回车触发就够。
 */
import { useState } from "react";
import { askKnowledge } from "../api";
import type { AskResponse } from "../types";

// 示例问题：点了直接提问。都是知识库里确实有数据的问题（见 scripts/eval_ask.py 的题库）
const EXAMPLES = [
  "价格低于 100 元的商品有哪些？",
  "谁说过关于爱的名言？",
  "关于智慧的名言作者是谁？",
];

interface Props {
  /**
   * 点来源时把任务 id 交出去，让上层（App）在主界面加载那个任务的
   * 结论和轨迹。这是 P4 验收线第 3 条要求的能力：
   * "答案带来源引用，可点回原始任务" —— 没有这一步，
   * 来源只是几行不可验证的文字，用户没法判断答案是查到的还是编的。
   */
  onOpenTask?: (taskId: string) => void | Promise<void>;
}

export default function AskPanel({ onOpenTask }: Props) {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<AskResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const ask = async (text: string) => {
    const q = text.trim();
    if (!q || busy) return;
    setQuestion(q);
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(await askKnowledge(q, 5));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    ask(question);
  };

  return (
    <section className="panel ask">
      <div className="ask-head">
        <h2>知识库问答</h2>
        <span className="ask-sub">先跑任务采集数据，再回来问</span>
      </div>

      {/* form + onSubmit：浏览器里回车自动提交，不用手写 keydown 监听 */}
      <form className="ask-form" onSubmit={onSubmit}>
        <input
          className="ask-input"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="问点什么，比如：无线鼠标多少钱？"
          maxLength={300}
          disabled={busy}
        />
        <button className="ask-btn" type="submit" disabled={busy || !question.trim()}>
          {busy ? "检索中…" : "提问"}
        </button>
      </form>

      <div className="ask-examples">
        {EXAMPLES.map((q) => (
          <button key={q} type="button" className="chip" onClick={() => ask(q)} disabled={busy}>
            {q}
          </button>
        ))}
      </div>

      {error && <p className="error">{error}</p>}

      {/* 后端降级（DB/LLM 不可用）：显示提示，不当作错误 —— 与统计看板同口径 */}
      {result && !result.available && <p className="hint">{result.note ?? "问答暂不可用"}</p>}

      {result?.available && (
        <>
          <div className={`ask-answer${result.note === "no_hits" ? " muted" : ""}`}>
            {result.answer}
          </div>

          {result.sources.length > 0 && (
            <div className="ask-sources">
              <div className="ask-sources-title">
                {/* 说明压到一行：左列只有 300px，长句会折成两行不好看 */}
                来源（[n] 对应答案编号，数字为相似度）
              </div>
              <ol>
                {result.sources.map((s) => (
                  <li key={s.index}>
                    <span className="ask-idx">[{s.index}]</span>
                    {/* title 属性：标题被省略号截断时，悬停能看全 */}
                    <span className="ask-src-title" title={s.title}>
                      {s.title}
                    </span>
                    {/* 语料类型 + 相似度。只留两位小数并把"相似度"一词提到标题行说明里，
                        因为左列只有 300px 宽，写全称会被按钮挤到换行，整排卡片高低不齐 */}
                    <span className="ask-src-meta" title={`相似度 ${s.similarity.toFixed(3)}`}>
                      {s.source_type === "page_text" ? "页面正文" : "任务问答"}{" "}
                      {s.similarity.toFixed(2)}
                    </span>
                    {/* 点"查看任务"→ 主界面加载该任务的结论和轨迹（验收线第 3 条） */}
                    <button
                      type="button"
                      className="ask-src-open"
                      onClick={() => onOpenTask?.(s.task_id)}
                      title={`查看任务 ${s.task_id} 的结论与轨迹`}
                    >
                      查看任务
                    </button>
                    {/* 原始页面外链：直接打开采集来源那一页，独立于本系统 */}
                    {s.url && (
                      <a className="ask-src-link" href={s.url} target="_blank" rel="noreferrer">
                        原始页面 ↗
                      </a>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </>
      )}
    </section>
  );
}
