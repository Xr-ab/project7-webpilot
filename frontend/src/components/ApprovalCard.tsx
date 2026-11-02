/**
 * 审批卡片（P5a-4）。
 *
 * 它在系统里的角色：把「Agent 想做什么」摆到人面前，并收集那个决定。
 * 后端在闸门停下时已经把决策上下文（动作 + 理由 + 暂停页 + 表单内容）冻结落库了，
 * 所以这张卡只读不推理——它不判断风险、不改写动作，只负责三件事：
 * 把事实说清楚、收一个 approve/reject、把结果告诉 App。
 *
 * 为什么卡片自己拉数据，而不是让 App 把审批项传下来：
 * App 的轮询循环关心的是「任务状态」，它一直在变；审批项一旦出现就是固定的那一份。
 * 两者生命周期不同，塞进同一个 effect 会让它同时管两件事——而那个 effect
 * 已经负责"什么时候该停轮询"了，再叠逻辑迟早出错。
 * 卡片只在 active 变成 true 时拉一次，**不轮询**：等人做决定的期间不该有后台请求空转。
 *
 * 为什么先显示 reason 再显示动作：
 * reason 是后端拼好的人话（"点击「提交」属于高风险动作（命中关键词：提交）"），
 * 而 action 里只有元素编号 e6——人看不懂 e6 是什么。所以人话在前，
 * 编号缩成一行小字当技术细节，用于事后和 trace 对账。
 */
import { useEffect, useState } from "react";
import { decideApproval, fetchApproval } from "../api";
import type { ApprovalDetail, PendingApproval } from "../types";

interface Props {
  /** 当前展示的任务 id */
  taskId: string;
  /** 只有任务处于 waiting_approval 时才为 true。变 true 拉一次，变 false 收起。 */
  active: boolean;
  /** 决定提交成功后通知外层：立刻重拉任务状态（否则要等下一次轮询才变） */
  onDecided: () => void;
}

/** 把动作渲染成一行技术描述。
 *
 * ⚠️ 刻意**不显示 type 动作的 text**：模型给密码框填的是真实密码，
 * 审批表里存的也是明文（这是 P5b 要处理的已知问题，见文档风险表）。
 * 卡片是给人看的第一现场，不该再把密码复述一遍。
 * 人要知道的是"它要往密码框里打字"，而这句话 reason 里已经有了。
 */
function technical(action: PendingApproval["action"]): string {
  switch (action.action) {
    case "click":
      return `click ${action.target ?? ""}`;
    case "type":
      // 只给位置，不给内容（理由见上）
      return `type ${action.target ?? ""} ← （内容不展示）`;
    case "select":
      return `select ${action.target ?? ""} = ${action.option ?? ""}`;
    case "goto":
      return `goto ${action.url ?? ""}`;
    default:
      return action.action;
  }
}

function formatWait(sec: number): string {
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟`;
  return `${Math.floor(sec / 3600)} 小时`;
}

export default function ApprovalCard({ taskId, active, onDecided }: Props) {
  // detail 为 null 有两种情况：还没拉到、以及刚提交完（立即收起，不等下一轮轮询）
  const [detail, setDetail] = useState<ApprovalDetail | null>(null);
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    // 任务不在等待审批：清空，避免下一次停下来时闪一眼上一条的旧内容
    if (!active) {
      setDetail(null);
      setNote("");
      setError("");
      return;
    }

    let stopped = false;
    setLoading(true);
    setError("");
    fetchApproval(taskId)
      .then((data) => {
        if (stopped) return;
        // pending 为 null 时留空：说明刚好被别处处理了，下一次轮询会看到最新状态
        setDetail(data.pending);
      })
      .catch((e: Error) => {
        if (!stopped) setError(e.message);
      })
      .finally(() => {
        if (!stopped) setLoading(false);
      });

    return () => {
      stopped = true;
    };
  }, [taskId, active]);

  const submit = async (decision: "approve" | "reject") => {
    setSubmitting(decision);
    setError("");
    try {
      await decideApproval(taskId, decision, note.trim());
      // 先本地收起：任务状态要等下一次轮询才变成 queued，不收的话卡片会带着
      // 还能点的按钮再挂几秒，用户会以为没点中而重复点（后端有 CAS 兜着，
      // 但让用户看到一次 409 报错是不必要的惊吓）
      setDetail(null);
      setNote("");
      onDecided();
    } catch (e) {
      // 409（已被处理）/ 503（队列不可用）都原样展示后端那句人话——
      // 它们各自对应不同的下一步动作，不该被压成"提交失败"
      setError((e as Error).message);
      // 409 说明这条已经不算数了：通知外层重拉，让界面跟上真实状态
      if ((e as Error).message.includes("已被处理")) onDecided();
    } finally {
      setSubmitting(null);
    }
  };

  if (!active) return null;

  if (loading && !detail) {
    return (
      <section className="panel approval">
        <p className="hint">正在读取待审批内容…</p>
      </section>
    );
  }

  // 拉到了但没有 pending：任务状态和审批表之间的瞬时不一致（刚好被处理完）。
  // 不报错，交给下一轮轮询自愈。
  if (!detail) {
    return (
      <section className="panel approval">
        {error ? <p className="error">{error}</p> : <p className="hint">没有待审批项了，正在刷新…</p>}
      </section>
    );
  }

  return (
    <section className="panel approval">
      <h2>需要你确认</h2>

      {/* 人话在前：reason 是后端拼好的，直接回答"它要干什么、为什么停" */}
      <p className="approval-ask">{detail.reason}</p>

      {/* 技术细节在后：和 trace 里的动作行对得上，用于事后核对 */}
      <p className="approval-tech mono">
        第 {detail.step_no} 步 · {technical(detail.action)}
      </p>

      <p className="approval-url mono" title={detail.snapshot_url}>
        暂停在：{detail.snapshot_url}
      </p>

      {/*
       * 计划里原本要求这里写「恢复后需重新填写表单」（当时的代价是表单必丢）。
       * **那条现在是错的**：P5a-3 用方案 D 解决了——暂停时把表单值冻结落库，
       * 续跑先回现场再回填。写"要重新填"会让用户以为点了批准自己的输入就白填了，
       * 反而不敢点。
       * 但确实有代价需要说：批准会重开一次浏览器（几百毫秒到几秒），
       * 页面会重新加载。不说的话，用户点了之后看到界面「转圈→任务重跑」
       * 会以为出问题了。
       * 密码框是唯一的例外（不落盘、不回填），所以不在这里展开——
       * 需要密码的任务本来就不该走这条路，P5b 之后应该用已保存的登录态。
       */}
      <p className="hint approval-resume-hint">
        批准后会重新打开这个页面、回填已填内容，再从这一步继续执行。
      </p>

      {detail.waiting_s !== null && (
        <p className="hint">已等待 {formatWait(detail.waiting_s)}</p>
      )}

      <label className="field approval-note">
        <span>备注 / 拒绝理由（选填，最多 500 字）</span>
        <textarea
          rows={2}
          value={note}
          maxLength={500}
          placeholder="例如：不要提交，只读列表就够了"
          onChange={(e) => setNote(e.target.value)}
          disabled={submitting !== null}
        />
      </label>
      <p className="hint approval-note-hint">
        拒绝时这段文字会直接告诉 Agent，让它换个做法；批准时它只作为审计备注。
      </p>

      <div className="approval-actions">
        <button
          type="button"
          className="run approval-approve"
          onClick={() => void submit("approve")}
          disabled={submitting !== null}
        >
          {submitting === "approve" ? "已提交…" : "批准执行"}
        </button>
        <button
          type="button"
          className="approval-reject"
          onClick={() => void submit("reject")}
          disabled={submitting !== null}
        >
          {submitting === "reject" ? "已提交…" : "拒绝并让它换个做法"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}
    </section>
  );
}
