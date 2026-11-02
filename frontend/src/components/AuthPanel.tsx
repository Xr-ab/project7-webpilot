/**
 * 登录 / 注册卡片（M4 认证的前端入口）。
 *
 * 为什么必须有这一层：后端从 M4 起所有任务接口默认要 token（AUTH_ENABLED=true），
 * 没有它前端连"提交任务"都是 401。这个卡片是整条链路的前置门槛。
 *
 * 交互上只做最低限度的两件事：登录、注册。不做"忘记密码"（本地项目没有邮件服务）、
 * 不做第三方登录（没意义），免得把注意力从 Agent 本体上挪开。
 */
import { useState } from "react";
import { login, register } from "../api";

export default function AuthPanel({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async (mode: "login" | "register") => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (mode === "login") {
        await login(email.trim(), password);
      } else {
        await register(email.trim(), password);
      }
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-wrap">
      <form
        className="panel auth"
        onSubmit={(e) => {
          e.preventDefault();
          run("login");
        }}
      >
        <h1>WebPilot</h1>
        <p className="auth-sub">浏览器操作 Agent · 登录后使用</p>

        <label className="auth-field">
          <span>邮箱</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            autoComplete="username"
            required
          />
        </label>

        <label className="auth-field">
          <span>密码（至少 8 位）</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            autoComplete="current-password"
            minLength={8}
            required
          />
        </label>

        {error && <p className="error">{error}</p>}

        <div className="auth-actions">
          <button className="ask-btn" type="submit" disabled={busy}>
            {busy ? "处理中…" : "登录"}
          </button>
          <button
            className="auth-secondary"
            type="button"
            onClick={() => run("register")}
            disabled={busy}
          >
            注册新账号
          </button>
        </div>

        <p className="hint">
          第一次用？填个邮箱和密码点「注册新账号」即可，数据存在本机 PostgreSQL 里。
        </p>
      </form>
    </div>
  );
}
