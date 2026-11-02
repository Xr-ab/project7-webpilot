/** 后端接口封装。所有 fetch 都收在这里，组件里不出现 URL 字符串。 */
import type {
  ApprovalResponse,
  AskResponse,
  CredentialListResponse,
  MyTasksResponse,
  SaveCredentialResult,
  SearchResult,
  StatsData,
  TaskMeta,
  TraceItem,
} from "./types";

// 相对路径：开发期由 Vite 代理到 8000，生产同理（同域部署）
const BASE = "/api/v1";

// ---- token 管理 ----
// 为什么存 localStorage 而不是内存：刷新页面不用重新登录。
// 为什么不用 httpOnly cookie：那需要后端配 CORS 凭证 + 同域部署，
// 本地开发（5173 → 8000 代理）下 localStorage 更省事。
// 代价：XSS 能偷走 token —— 这个项目的威胁模型是本地单机，可接受；
// 真要上生产，应该换 httpOnly cookie + CSRF token。
const TOKEN_KEY = "webpilot_token";
const EMAIL_KEY = "webpilot_email";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function getEmail(): string {
  return localStorage.getItem(EMAIL_KEY) ?? "";
}

export function setAuth(token: string, email: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(EMAIL_KEY, email);
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EMAIL_KEY);
}

export function isLoggedIn(): boolean {
  return getToken() !== "";
}

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    // FastAPI 的错误体是 {detail: "..."}，把 detail 抛出来，前端才知道是白名单还是 404
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* 非 JSON 错误体，保持默认文案 */
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

/**
 * 带 token 的 fetch。所有业务请求都走这里。
 *
 * 401 统一处理：清掉本地 token 并抛明确错误，让界面回到登录态。
 * 不这么做的话，token 过期后每个请求都报"未登录"，用户不知道该怎么办。
 */
async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 401) {
    clearAuth();
    throw new Error("登录已失效，请重新登录");
  }
  return resp;
}

// ---- 认证 ----

interface AuthResult {
  user_id: number;
  email: string;
  token: string;
}

async function authCall(path: string, email: string, password: string): Promise<AuthResult> {
  const resp = await fetch(`${BASE}/auth${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await json<AuthResult>(resp);
  setAuth(data.token, data.email);
  return data;
}

/** 登录。失败抛出后端的中文提示（"email 或密码不对"）。 */
export function login(email: string, password: string): Promise<AuthResult> {
  return authCall("/login", email, password);
}

/** 注册。已注册过时后端返回 409 + "该 email 已注册，直接登录即可"。 */
export function register(email: string, password: string): Promise<AuthResult> {
  return authCall("/register", email, password);
}

// ---- 任务 ----

export async function submitTask(
  goal: string,
  startUrl: string,
  /**
   * P5b：用哪份登录态开局。null/省略 = 匿名访问（P5b 之前的行为）。
   *
   * 为什么这里传的是 id 而不是账号密码：密码在"保存登录态"那一步就用完即弃了，
   * 这个请求生命周期里根本没有密码可传。这不是约定，是**接口形状**上就不给这条路。
   */
  credentialId?: number | null,
): Promise<string> {
  const resp = await authFetch("/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      goal,
      start_url: startUrl,
      credential_id: credentialId ?? null,
    }),
  });
  const data = await json<{ task_id: string }>(resp);
  return data.task_id;
}

export async function fetchTask(taskId: string): Promise<TaskMeta> {
  return json<TaskMeta>(await authFetch(`/tasks/${taskId}`));
}

export async function fetchTrace(taskId: string): Promise<TraceItem[]> {
  const data = await json<{ trace: TraceItem[] }>(await authFetch(`/tasks/${taskId}/trace`));
  return data.trace;
}

/**
 * 查任务当前的待审批项（P5a-4 卡片的数据源）。
 *
 * 为什么要单独一次请求，而不是直接用 `meta.approval`：
 * meta 里那份是 workflow 直出的五键快照，没有 id / created_at / waiting_s——
 * 卡片要显示"已等待多久"就缺数据。这个接口读的是表，形状才是权威的。
 * 而且它和 `/tasks/{id}` 一样带归属校验：别人的任务照样 404。
 */
export async function fetchApproval(taskId: string): Promise<ApprovalResponse> {
  return json<ApprovalResponse>(await authFetch(`/tasks/${taskId}/approval`));
}

/**
 * 提交人的决定（P5a-4）。
 *
 * approve 和 reject **都会**让任务重新排队：批准去执行那个动作，
 * 拒绝把理由回喂模型让它换个做法。所以两种决定成功后都是 `task_status = "queued"`，
 * 前端都该继续轮询——别把 reject 当"结束"。
 *
 * 失败语义（由后端约定）：
 * - 409 已经处理过 / 任务不在等待 → 刷新看看最新状态，不是参数错
 * - 503 队列不可用 → 决定没白做（审批项已回滚），可以再点一次
 */
export async function decideApproval(
  taskId: string,
  decision: "approve" | "reject",
  note = "",
): Promise<{ task_status: string; decision: string; note: string }> {
  const resp = await authFetch(`/tasks/${taskId}/approval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note }),
  });
  return json(resp);
}

/**
 * 我的任务列表（M4 收尾）。
 *
 * 为什么要这个接口：在后端做出来之前，"隔离"只是个看不见的承诺——
 * 换个账号登录，页面上完全一样。有了列表，A 有 3 条、B 是空的，
 * 隔离就是肉眼可验证的事实。
 *
 * DB 不可用时后端返回 200 + available:false + 空列表（与 /stats 同口径），
 * 所以这里不抛错，调用方按"不可用"渲染。
 */
export async function fetchMyTasks(limit = 10): Promise<MyTasksResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  return json<MyTasksResponse>(await authFetch(`/tasks?${params}`));
}

/**
 * 聚合统计（M5c 看板的数据源）。
 * DB 不可用时后端返回 200 + available:false（不抛错），调用方按"不可用"渲染提示。
 */
export async function fetchStats(): Promise<StatsData> {
  return json<StatsData>(await authFetch("/stats"));
}

// ---- 知识库（P4） ----

/**
 * 语义检索。返回按加权分排序的片段（页面级优先于任务级摘要）。
 * 调试用；问答走 askKnowledge。
 */
export async function searchKnowledge(
  query: string,
  topK = 5,
): Promise<{ available: boolean; note?: string; count: number; results: SearchResult[] }> {
  const params = new URLSearchParams({ q: query, top_k: String(topK) });
  return json(await authFetch(`/knowledge/search?${params}`));
}

/**
 * 知识库问答：检索 + 生成，返回答案与来源列表。
 * 后端失败时返回 200 + available:false（不抛错），所以这里不 try/catch 也能拿到提示。
 */
export async function askKnowledge(question: string, topK = 5): Promise<AskResponse> {
  const resp = await authFetch("/knowledge/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, top_k: topK }),
  });
  return json<AskResponse>(resp);
}

/**
 * 读取"知识来源任务"的结论与轨迹（只读）。
 *
 * 为什么不复用 fetchTask：`/api/v1/tasks/{id}` 做了用户归属校验，
 * 而知识库是全局共享的——用采集账号跑出来的知识，点来源时当前账号并不拥有那个任务，
 * 会直接 404。这个接口只放行"确实被知识库收录过的任务"，
 * 既保证溯源可用，又不等于放开任意任务的读取权限。
 */
export async function fetchKnowledgeTask(
  taskId: string,
): Promise<{ meta: TaskMeta; trace: TraceItem[]; readonly: boolean }> {
  return json(await authFetch(`/knowledge/tasks/${taskId}`));
}

/** 语音转写：把一段录音上传，换回文字。 */
export async function transcribeAudio(blob: Blob): Promise<{ text: string; note?: string }> {
  const form = new FormData();
  // 第三个参数是文件名。浏览器按扩展名猜 Content-Type，
  // 后端也靠它兜底判断——不给名字会变成 "blob" 没有扩展名，某些代理会拒绝。
  form.append("file", blob, "voice.webm");

  // 注意：这里**绝对不能**手动设 Content-Type。
  // multipart 的 Content-Type 里带一段随机 boundary，必须让浏览器自己生成；
  // 手写 "multipart/form-data" 会丢掉 boundary，后端解析直接报 400。
  // authFetch 只往 Headers 里塞 Authorization，不碰 Content-Type，所以是安全的。
  const resp = await authFetch("/voice/transcribe", { method: "POST", body: form });
  return json<{ text: string; note?: string }>(resp);
}

export interface SpeechResult {
  url: string | null;
  text: string;
  cached: boolean;
  ok?: boolean;
  note?: string;
}

/** 请求为某个任务的结论合成语音。失败时返回 ok=false 而不是抛错（语音是可选功能）。 */
export async function requestSpeech(taskId: string): Promise<SpeechResult> {
  return json<SpeechResult>(await authFetch(`/tasks/${taskId}/speech`, { method: "POST" }));
}

// ---- 登录态（P5b） ----

/**
 * 保存一份登录态：后端拿账号密码**代替用户登录一次**，导出会话并加密存起来。
 *
 * 🔴 这个函数是前端唯一出现明文密码的地方，所以它的三条纪律写在最前面：
 * 1. 密码只在这一次请求体里出现，函数返回后调用方必须立刻清空输入框的 state
 * 2. 出错时不把请求体拼进错误信息（后端也不会回显，但前端不该自己补上）
 * 3. 这个调用是**同步等**的（后端会真的开浏览器登录，2~5 秒），
 *    所以按钮要有 loading 态——不然用户以为没点上，会连点几次，
 *    每次都在后端起一次 Chromium。
 */
export async function saveCredential(input: {
  loginUrl: string;
  username: string;
  password: string;
  label?: string;
}): Promise<SaveCredentialResult> {
  const resp = await authFetch("/credentials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      login_url: input.loginUrl,
      username: input.username,
      password: input.password,
      label: input.label ?? "",
    }),
  });
  return json<SaveCredentialResult>(resp);
}

/**
 * 列出我保存的登录态（不含密文）。
 * DB 不可用时后端返回 200 + available:false（和 /stats 同口径），所以这里不抛错。
 */
export async function fetchCredentials(): Promise<CredentialListResponse> {
  return json<CredentialListResponse>(await authFetch("/credentials"));
}

/** 删掉一份。别人的 id 后端返回 404（不是 403，避免泄露 id 存在性）。 */
export async function deleteCredential(id: number): Promise<void> {
  const resp = await authFetch(`/credentials/${id}`, { method: "DELETE" });
  await json<{ deleted: number }>(resp);
}

/** 预设任务：和 scripts/agent_demo.py、M6 评测用的是同一套口径。 */
export const PRESETS = [
  { label: "T1 导航+提取", goal: "从首页进入产品列表，提取所有产品名称", url: "/testsite/index.html" },
  { label: "T2 表单提交", goal: "打开资料填写页，姓名填张三、邮箱填 zhang@example.com、城市选福州、勾选订阅，然后提交", url: "/testsite/form.html" },
  { label: "T3 读表判断", goal: "在产品列表页找出所有价格低于 100 元的商品名称", url: "/testsite/list.html" },
  { label: "T4 搜索计数", goal: "在商品搜索页搜索 usb，告诉我一共几条结果", url: "/testsite/search.html" },
  { label: "T5 多步翻页", goal: "打开商品详情页，点下一页，告诉我第二页商品的名字和价格", url: "/testsite/detail.html" },
];
