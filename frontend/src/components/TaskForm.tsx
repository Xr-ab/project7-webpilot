import { useEffect, useState } from "react";
import { PRESETS, fetchCredentials } from "../api";
import type { CredentialItem } from "../types";
import VoiceButton from "./VoiceButton";

interface Props {
  running: boolean;
  error: string;
  /** P5b：credentialId 为 null 表示匿名访问 */
  onRun: (goal: string, startUrl: string, credentialId: number | null) => void;
  /**
   * 登录态列表的刷新信号。
   * 为什么由外层传：保存/删除发生在 CredentialPanel（同级组件），
   * 它改了数据之后这个下拉必须跟着变，否则会出现"刚保存了却选不到"。
   * 用一个自增数字而不是共享状态：两个组件只关心"要不要刷"，不关心数据本身。
   */
  credRefresh?: number;
}

/** 任务输入区：预设任务一键填充 + 自由输入 + 用哪份登录态。 */
export default function TaskForm({ running, error, onRun, credRefresh = 0 }: Props) {
  // 用绝对地址做默认值：相对路径在 <input> 里不好看，也容易让人以为能填别的站
  const origin = window.location.origin;
  const [goal, setGoal] = useState(PRESETS[0].goal);
  const [url, setUrl] = useState(origin + PRESETS[0].url);

  // P5b：可选的登录态。null = 匿名（默认，和 P5b 之前的行为一致）
  const [creds, setCreds] = useState<CredentialItem[]>([]);
  const [credId, setCredId] = useState<number | null>(null);

  useEffect(() => {
    let stopped = false;
    fetchCredentials()
      .then((data) => {
        if (stopped) return;
        const items = data.items ?? [];
        setCreds(items);
        setCredId((current) => {
          // ① 选中的那份被删了（或第一次加载）：不能继续指向一个不存在的 id，
          //    否则提交时后端 404，用户看到的是一句和"删了登录态"毫无关系的报错。
          if (current !== null && items.some((i) => i.id === current)) return current;
          // ② 只有一份时直接选中它。用户存登录态本来就是为了用，
          //    而"只有一份"不存在选错的可能；多份时一律回到匿名，让他自己选。
          if (items.length === 1) return items[0].id;
          return null;
        });
      })
      // 拉失败就当作"没有登录态"：这是可选增强项，不该拦住整个任务表单
      .catch(() => {
        if (!stopped) setCreds([]);
      });
    return () => {
      stopped = true;
    };
  }, [credRefresh]);

  const applyPreset = (index: number) => {
    const preset = PRESETS[index];
    setGoal(preset.goal);
    setUrl(origin + preset.url);
  };

  return (
    <aside className="panel form">
      <h2>任务</h2>

      <label className="field">
        <span>预设</span>
        <select disabled={running} onChange={(e) => applyPreset(Number(e.target.value))}>
          {PRESETS.map((preset, index) => (
            <option key={preset.label} value={index}>
              {preset.label}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>要它做什么</span>
        <textarea
          rows={5}
          value={goal}
          disabled={running}
          onChange={(e) => setGoal(e.target.value)}
        />
      </label>

      {/* 语音输入：转写完**只是填进上面的输入框**，不直接提交。
          为什么不让它自动跑：ASR 一定会错字（"usb" 常被识别成 "USB" 或 "us 币"），
          语音指令又往往带口语（"帮我看看那个页面有几条"），
          直接提交等于把一个有错的文本交给 Agent 去猜。让用户看一眼、改一下，成本 2 秒。 */}
      <VoiceButton disabled={running} onText={setGoal} />

      <label className="field">
        <span>从哪个页面开始</span>
        <input value={url} disabled={running} onChange={(e) => setUrl(e.target.value)} />
      </label>

      {/* P5b：用哪个身份访问。放在"起始地址"下面是有意的——
          它俩是一对：先决定去哪儿，再决定以谁的身份去。
          过期的也不禁用：cookie 的 expires 只是个声明，很多站点实际还能用，
          拦掉反而会让人为了试一下而去重新登录一遍。 */}
      <label className="field">
        <span>用哪个登录态</span>
        <select
          disabled={running}
          value={credId === null ? "" : String(credId)}
          onChange={(e) => setCredId(e.target.value === "" ? null : Number(e.target.value))}
        >
          <option value="">匿名访问（不带登录态）</option>
          {creds.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}（{item.site}）{item.expired ? " · 可能已过期" : ""}
            </option>
          ))}
        </select>
      </label>
      {creds.length === 0 && (
        <p className="hint">
          还没有保存登录态。需要访问登录后才看得到的页面时，在上面「登录态」里存一份。
        </p>
      )}

      <button
        className="run"
        disabled={running || !goal.trim()}
        onClick={() => onRun(goal, url, credId)}
      >
        {running ? "执行中…" : "开始执行"}
      </button>

      {error && <p className="error">{error}</p>}

      <p className="hint">白名单：只能操作 127.0.0.1 / localhost 下的页面。</p>
    </aside>
  );
}
