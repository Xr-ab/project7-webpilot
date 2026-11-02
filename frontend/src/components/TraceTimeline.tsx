import { useEffect, useRef } from "react";
import type { TraceItem } from "../types";

interface Props {
  items: TraceItem[];
  running: boolean;
}

/** 轨迹时间线：每步一条，thought 是"它当时怎么想的"，截图是"它当时看到的"。 */
export default function TraceTimeline({ items, running }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // 新步骤出现时自动滚到底部 —— 不然跑到第 8 步，用户还停在第 1 步
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items.length]);

  if (items.length === 0) {
    return (
      <section className="panel trace">
        <h2>轨迹</h2>
        <p className="muted">{running ? "正在打开页面…" : "执行后会在这里逐步显示。"}</p>
      </section>
    );
  }

  return (
    <section className="panel trace">
      <h2>
        轨迹 <span className="muted">共 {items.length} 步</span>
      </h2>

      <ol className="steps">
        {items.map((item) => (
          <li key={item.step} className={item.ok ? "step" : "step failed"}>
            <div className="step-head">
              <span className="num">{item.step}</span>
              <code className="action">{item.action}</code>
              <span className="ms">{item.duration_ms}ms</span>
            </div>

            {item.thought && <p className="thought">{item.thought}</p>}
            {item.error && <p className="step-error">{item.error}</p>}

            {item.screenshot_url && (
              <a href={item.screenshot_url} target="_blank" rel="noreferrer" className="shot">
                <img src={item.screenshot_url} alt={`第 ${item.step} 步截图`} loading="lazy" />
              </a>
            )}
          </li>
        ))}
      </ol>

      <div ref={bottomRef} />
    </section>
  );
}
