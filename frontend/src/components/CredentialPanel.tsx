/**
 * 登录态面板（P5b）。
 *
 * 它在系统里的角色：把「Agent 没有身份」这件事变成用户能点一下解决的操作。
 * 用户在这里填一次账号密码 → 后端代他登录一次 → 导出会话加密存起来 →
 * 之后提任务时选一份，Agent 开局就是登录态，**不需要也不应该在任务里填密码**。
 *
 * 🔴 这个文件是整个前端唯一出现明文密码的地方，所以密码纪律写在这里：
 *
 * 1. **不进任何日志**：这个文件里没有一句 console.log，也不许加。
 *    （调试列表数据时只打 id 和 label，别顺手 console.log(form)）
 * 2. **用完立刻清**：保存请求一返回——成功或失败都算——立刻 `setPassword("")`。
 *    失败不清是最容易漏的：用户改个密码重试，旧密码还躺在 state 里。
 * 3. **不进 URL / 不进 localStorage**：只有 POST body 这一条路。
 *    所以这里没有"记住密码"选项，也不该有。
 * 4. `autoComplete="new-password"` 而不是 "current-password"：后者会让浏览器
 *    把**工作台的登录密码**自动填进这个框，用户一不留神就把它当成站点密码提交了。
 *
 * 为什么保存在这里而不用 `<details>` 折叠：
 * 左列已经有任务列表、任务表单、知识问答三块，这块再常驻展开会把表单挤出首屏。
 * 用 state 控制而不是 details，是因为保存成功后要**自动展开**（让用户看见结果），
 * details 的 open 属性也能做，但和 React 状态混用容易两头不一致。
 */
import { useEffect, useState } from "react";
import { deleteCredential, fetchCredentials, saveCredential } from "../api";
import type { CredentialItem } from "../types";

interface Props {
  /** 保存/删除成功后通知外层：任务表单的"用哪个登录态"下拉要重新拉一遍 */
  onChanged: () => void;
}

/** 后端的时间是 "2026-10-26 18:08:31"，这里只留到分钟，够用且省宽度。 */
function shortTime(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 16);
}

/** 一句话说清"这份会话还能不能用"。三种情况分开写，别压成一句"有效期"。 */
function expiryText(item: CredentialItem): string {
  if (item.expired) return "已过期，建议重新保存";
  if (item.expires_at) return `有效至 ${shortTime(item.expires_at)}`;
  // 会话型 cookie：Playwright 用 -1 表示，后端算不出时间就返回 null。
  // 这种情况必须说清楚，否则用户会以为"存一次能一直用"。
  return "会话型（关浏览器即失效）";
}

export default function CredentialPanel({ onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<CredentialItem[]>([]);
  const [available, setAvailable] = useState(true);
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);

  // 表单状态。默认登录页指向本地测试站——这个项目的日常就是在这里验登录。
  const [loginUrl, setLoginUrl] = useState(`${window.location.origin}/testsite/login.html`);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [label, setLabel] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const data = await fetchCredentials();
      setItems(data.items ?? []);
      setAvailable(data.available);
      setNote(data.note ?? "");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  // 收起时也拉一次：标题上要显示"N 份"，不拉就永远是 0，
  // 用户会以为"我明明存过"。这个请求很轻（一行 select），不必懒加载。
  useEffect(() => {
    void load();
  }, []);

  const submit = async () => {
    setError("");
    setSaved("");
    setSaving(true);
    try {
      const result = await saveCredential({
        loginUrl: loginUrl.trim(),
        username: username.trim(),
        password,
        label: label.trim(),
      });
      setSaved(
        `已保存「${result.label}」（${result.cookie_count} 条 cookie）` +
          (result.note ? ` · ${result.note}` : ""),
      );
      // 清掉密码和账号：账号不是密钥，但既然保存成功了，再让它们占着输入框
      // 只会让人误以为"还没存进去"。
      setPassword("");
      setUsername("");
      setLabel("");
      await load();
      onChanged();
    } catch (e) {
      // 后端会把"账号或密码可能不对""找不到登录按钮"这类人话放进 detail，
      // 原样展示——这些是用户能自己改的问题，压成"保存失败"等于没说。
      setError((e as Error).message);
    } finally {
      // 无论成败都清密码。失败时留着旧密码，用户改了重试会分不清哪个被提交了。
      setPassword("");
      setSaving(false);
    }
  };

  const remove = async (item: CredentialItem) => {
    setError("");
    setSaved("");
    try {
      await deleteCredential(item.id);
      await load();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <aside className={`panel credential${open ? " open" : ""}`}>
      <button
        type="button"
        className="credential-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <h2>登录态</h2>
        <span className="credential-count">
          {loading ? "…" : items.length > 0 ? `${items.length} 份` : "未保存"}
        </span>
        <span className="credential-caret">{open ? "收起" : "管理"}</span>
      </button>

      {!open && (
        <p className="hint credential-hint">
          存一份登录态，Agent 就能直接进需要登录的页面，不必在任务里填密码。
        </p>
      )}

      {open && (
        <>
          {!available && (
            <p className="note">
              数据库不可用，读不到也存不了登录态{note ? `（${note}）` : ""}。
            </p>
          )}

          {items.length > 0 && (
            <ul className="credential-list">
              {items.map((item) => (
                <li key={item.id} className={item.expired ? "expired" : ""}>
                  <div className="credential-row">
                    <span className="credential-label">{item.label}</span>
                    <button
                      type="button"
                      className="link-btn credential-del"
                      onClick={() => void remove(item)}
                      title="删掉这份登录态"
                    >
                      删除
                    </button>
                  </div>
                  <div className="credential-meta mono">{item.site}</div>
                  <div className="credential-meta">{expiryText(item)}</div>
                  <div className="credential-meta">
                    {item.last_used_at
                      ? `最近使用 ${shortTime(item.last_used_at)}`
                      : "还没被任务用过"}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {items.length === 0 && available && (
            <p className="hint">还没有保存任何登录态。下面填一次，之后就能重复用。</p>
          )}

          <div className="credential-form">
            <label className="field">
              <span>登录页地址</span>
              <input
                value={loginUrl}
                disabled={saving || !available}
                onChange={(e) => setLoginUrl(e.target.value)}
              />
            </label>

            <label className="field">
              <span>站点账号</span>
              <input
                value={username}
                disabled={saving || !available}
                autoComplete="off"
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>

            <label className="field">
              <span>站点密码</span>
              <input
                type="password"
                value={password}
                disabled={saving || !available}
                // new-password 而不是 current-password：后者会让浏览器把
                // **工作台的**密码自动填进来（见文件头第 4 条）
                autoComplete="new-password"
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>

            <label className="field">
              <span>显示名（选填，默认用账号名）</span>
              <input
                value={label}
                disabled={saving || !available}
                placeholder="例如：测试站-张三"
                onChange={(e) => setLabel(e.target.value)}
              />
            </label>

            <button
              type="button"
              className="run"
              disabled={saving || !available || !username.trim() || !password}
              onClick={() => void submit()}
            >
              {/* 说清"为什么要等"：这一步后端真的会开一次浏览器去登录，
                  不解释的话用户看到 3 秒转圈会以为卡死了 */}
              {saving ? "正在登录并保存…（要开一次浏览器）" : "登录并保存"}
            </button>

            <p className="hint credential-note">
              密码只用于这次登录，登录完就丢掉；系统存的是会话（cookie），不是密码。
            </p>
          </div>

          {error && <p className="error">{error}</p>}
          {saved && <p className="note">{saved}</p>}
        </>
      )}
    </aside>
  );
}
