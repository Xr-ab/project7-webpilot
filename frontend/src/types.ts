/** 与后端接口一一对应的类型。手写而不自动生成：字段少，改的时候能立刻发现哪里对不上。 */

export type TaskStatus =
  | "queued"
  | "running"
  /** P5a：高风险动作停住等人批。既不是完成也不是失败，见后端 app/status.py */
  | "waiting_approval"
  | "success"
  | "step_limit"
  | "timeout"
  | "loop"
  | "invalid_action"
  | "bad_request"
  | "bad_url"
  | "nav_failed"
  | "interrupted"
  | "error";

/** P5a：待审批详情。挂在 TaskMeta.approval 上，来自后端 meta（P5a-2 后改读表）。 */
export interface PendingApproval {
  step_no: number;
  /** 为什么停：给用户看的解释，例如「点击「提交」属于高风险动作（命中关键词：提交）」 */
  reason: string;
  /** 待执行的动作（Action.model_dump）。P5a-1 只用于展示，P5a-3 恢复时直接执行它 */
  action: {
    thought?: string;
    action: string;
    target?: string | null;
    text?: string | null;
    option?: string | null;
    url?: string | null;
  };
  /** 暂停时的页面地址：恢复要重新 goto 它（浏览器活体不可序列化） */
  snapshot_url: string;
  context: {
    goal: string;
    plan: string[];
    progress: number[];
    current_url: string;
    step_no: number;
  };
}

export interface TaskMeta {
  task_id: string;
  goal: string;
  start_url: string;
  status: TaskStatus;
  model: string;
  steps: number;
  answer: string;
  note: string;
  created_at: string;
  finished_at: string | null;
  elapsed_s: number | null;
  /** 只有 status=waiting_approval 时才有 */
  approval?: PendingApproval | null;
  /**
   * P5b：这一趟用的是哪份登录态。**没有这个字段 = 匿名执行**。
   *
   * 为什么它不在 TaskListItem 里：它是"这次执行的现场"，不是任务属性。
   * 后端也只把它写进 meta.json（tasks 表没有这一列），所以刷新很久之后
   * 再打开老任务，读到的可能是当时的那一刻——这正是我们要的，
   * 它用来解释"为什么那次它看到的是未登录页面"。
   */
  credential?: RunCredential;
}

/** 本次执行使用登录态的结果。ok=false 时任务照跑，只是按未登录算。 */
export interface RunCredential {
  credential_id?: number;
  /** 登录态真的注入进去了吗 */
  ok: boolean;
  /** 显示名（成功时有） */
  label?: string;
  site?: string;
  /** 人话说明：为什么没注入 / 为什么可能失效。空串表示没有任何要说的 */
  note?: string;
}

/**
 * 审批详情（GET /tasks/{id}/approval 的 pending）。
 *
 * 为什么不是直接复用 PendingApproval：那份是 meta.json 里的形状（P5a-1），
 * 只有业务五键。表里有 id / created_at / waiting_s 这些"审计字段"，
 * 卡片要显示"已等待多久"，就必须认扩展后的形状。
 * 用继承而不是并列定义：前者保证两边不会字段漂移。
 */
export interface ApprovalDetail extends PendingApproval {
  /** 表写成功才有；DB 不可用时回退读 meta，这里就是 null */
  id: number | null;
  task_id: string;
  /** 审批项自己的状态（pending/approved/rejected/cancelled），和任务状态是两套 */
  status: string;
  /** 人的审批意见。拒绝时它就是回喂给模型的理由 */
  note: string;
  decided_by: number | null;
  created_at: string | null;
  /** 已等待秒数。回退读 meta 时算不出来，为 null（显示"未知"而不是编一个） */
  waiting_s: number | null;
}

/** GET /tasks/{id}/approval 的响应。pending=null 是正常结果：任务存在但没东西要批。 */
export interface ApprovalResponse {
  task_id: string;
  /** 顺手回传任务状态，省掉一次 GET /tasks/{id} */
  status: TaskStatus | null;
  pending: ApprovalDetail | null;
}

export interface TraceItem {
  step: number;
  url: string;
  thought: string;
  action: string;
  ok: boolean;
  error: string | null;
  duration_ms: number;
  elements_total: number;
  screenshot: string | null;
  screenshot_url?: string;
  llm_usage?: { prompt_tokens?: number; completion_tokens?: number };
}

/** 状态 -> 中文 + 颜色档位。失败态单独归成红档，成功绿档，进行中蓝档。 */
export const STATUS_TEXT: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "执行中",
  waiting_approval: "待审批",
  success: "已完成",
  step_limit: "失败：步数用尽",
  timeout: "失败：超时",
  loop: "失败：原地打转",
  invalid_action: "失败：动作非法",
  bad_request: "失败：参数不对",
  bad_url: "失败：地址不在白名单",
  nav_failed: "失败：页面打不开",
  interrupted: "失败：已中断",
  error: "失败：内部错误",
};

/** 颜色档位。warn 是 P5a 新增：待审批既不是成功也不是失败，用琥珀色单独提示。 */
export function statusTone(status: TaskStatus): "idle" | "run" | "ok" | "bad" | "warn" {
  if (status === "success") return "ok";
  if (status === "queued" || status === "running") return "run";
  if (status === "waiting_approval") return "warn";
  return "bad";
}

/** /api/v1/stats 的响应。字段和后端 repo.fetch_stats 一一对应。 */
export interface StatsData {
  available: boolean;
  note?: string;
  since?: string | null;
  total: number;
  unfinished: number;
  success: number;
  success_rate: number;
  avg_steps: number;
  elapsed_p50_s: number;
  elapsed_p95_s: number;
  tokens: { prompt: number; completion: number; total: number };
  status_distribution: Record<string, number>;
  by_goal: Array<{
    goal: string;
    total: number;
    success: number;
    success_rate: number;
    avg_steps: number;
    avg_elapsed_s: number;
  }>;
}

// ---- 知识库（P4） ----

/** 来源类型：任务问答 / 页面正文块。与后端 service.SOURCE_* 一致。 */
export type KnowledgeSourceType = "task_answer" | "page_text";

export interface SearchResult {
  task_id: string;
  source_type: KnowledgeSourceType;
  title: string;
  url: string;
  content: string;
  /** 原始余弦相似度：衡量"语义多接近" */
  similarity: number;
  /** 加权后的排序分：页面级 1.0、任务级 0.85（衡量"该不该排前面"） */
  score: number;
  meta: Record<string, unknown>;
}

/** 答案引用的来源。[index] 与答案文本里的 [n] 对应。 */
export interface AskSource {
  index: number;
  task_id: string;
  title: string;
  url: string;
  source_type: KnowledgeSourceType;
  similarity: number;
}

/** POST /api/v1/knowledge/ask 的响应。available=false 是后端降级（DB/LLM 不可用）。 */
export interface AskResponse {
  available: boolean;
  /** 后端提示：降级原因，或 "no_hits"（知识库里没资料，直接拒答而不是让模型编） */
  note?: string;
  answer: string;
  sources: AskSource[];
  hits: SearchResult[];
}

// ---- 我的任务列表（M4 收尾） ----

/** 列表项：只够渲染一行，不带 plan/progress 这些重字段。 */
export interface TaskListItem {
  task_id: string;
  goal: string;
  start_url: string;
  status: string;
  steps: number;
  answer: string;
  created_at: string;
  elapsed_s: number | null;
}

/** GET /api/v1/tasks 的响应。available=false 是后端降级（DB 不可用）。 */
export interface MyTasksResponse {
  available: boolean;
  note?: string;
  items: TaskListItem[];
  total: number;
}

// ---- 登录态（P5b） ----

/**
 * 一份保存好的登录态。**注意这里没有 ciphertext 字段，而且永远不会有**——
 * 后端列表接口刻意不返回密文（能解出 cookie 的令牌没必要在网络里多跑一遍）。
 * 如果哪天这里出现了密文，说明后端漏了 `with_ciphertext` 的默认值，要当 bug 处理。
 */
export interface CredentialItem {
  id: number;
  /** 登录页所属站点（只有 host，不含端口） */
  site: string;
  /** 显示名，默认是账号名 */
  label: string;
  created_at: string | null;
  /** 最近一次被任务用到的时刻。没被用过就是 null */
  last_used_at: string | null;
  /** 会话的声明过期时间。null = 会话型 cookie（关浏览器即失效） */
  expires_at: string | null;
  /**
   * 后端算好的过期判定。
   * 前端**绝不自己拿 expires_at 比时间**：两处各算一遍必然漂移，
   * 而"列表说没过期、执行时说过期"这种矛盾没人能查。
   */
  expired: boolean;
}

/** GET /api/v1/credentials 的响应。available=false 是后端降级（DB 不可用）。 */
export interface CredentialListResponse {
  available: boolean;
  note?: string;
  items: CredentialItem[];
}

/** POST /api/v1/credentials 的响应。只有元信息，没有任何会话内容。 */
export interface SaveCredentialResult {
  id: number;
  site: string;
  label: string;
  expires_at: string | null;
  expired: boolean;
  cookie_count: number;
  /** 后端给的提示，例如"这份是会话型 cookie（关浏览器即失效）" */
  note: string;
}
