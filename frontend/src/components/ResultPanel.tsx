import { useEffect, useState } from "react";
import { requestSpeech } from "../api";
import type { RunCredential, TaskMeta } from "../types";
import { STATUS_TEXT, statusTone } from "../types";

interface Props {
  meta: TaskMeta | null;
  /** 播报要按任务取音频，所以这里需要带上 task_id（App 里已经有，不用再查一次）。 */
  taskId: string | null;
}

/**
 * 本次执行用的是哪份登录态（P5b）。
 *
 * 为什么要显示它：不显示的话，"Agent 看到的是未登录页面"这件事对用户是完全
 * 不可解释的——他会以为是站点坏了、或者 Agent 太笨。而后端其实已经把原因
 * 写在 note 里了（"这份登录态已经不在了""无法解密，请重新保存"），
 * 这一行的作用就是把那句话摆到正确的位置。
 *
 * 三档：
 * - 没生效（ok=false）：琥珀色，说明任务照跑但按未登录算
 * - 生效但有事要说（note 非空，通常是"可能已过期"）：琥珀色轻提示
 * - 生效且没事：灰色一行，只是让人确认"确实带上了"
 */
function CredentialBadge({ credential }: { credential: RunCredential }) {
  if (!credential.ok) {
    return (
      <p className="run-credential warn">
        登录态未生效{credential.note ? `：${credential.note}` : ""}
      </p>
    );
  }
  return (
    <p className={`run-credential${credential.note ? " warn" : ""}`}>
      登录态：{credential.label || `#${credential.credential_id ?? "?"}`}
      {credential.site ? ` @ ${credential.site}` : ""}
      {credential.note ? ` · ${credential.note}` : ""}
    </p>
  );
}

/** 结论区：状态 / 答案 / 步数 / 耗时 / 失败说明 / 语音播报。 */
export default function ResultPanel({ meta, taskId }: Props) {
  // 注意：hooks 必须写在最上面，不能写在 `if (!meta) return` 之后。
  // React 靠"每次渲染 hook 调用顺序一致"来匹配状态，
  // 条件调用 hook 会让顺序错位，报 "Rendered fewer hooks than expected"。
  const [audioUrl, setAudioUrl] = useState("");
  const [speechLoading, setSpeechLoading] = useState(false);
  const [speechError, setSpeechError] = useState("");

  // 换任务时把上一轮的音频清掉。
  // 不清的话会出现"任务A的结论配着任务B的音频"，而且 <audio> 还缓存着旧 src。
  useEffect(() => {
    setAudioUrl("");
    setSpeechError("");
    setSpeechLoading(false);
  }, [taskId]);

  if (!meta) {
    return (
      <section className="panel result empty">
        <p>还没有任务。左边填个任务，点「开始执行」。</p>
      </section>
    );
  }

  const tone = statusTone(meta.status);
  const running = meta.status === "queued" || meta.status === "running";

  const speak = async () => {
    if (!taskId) return;
    setSpeechLoading(true);
    setSpeechError("");
    try {
      const result = await requestSpeech(taskId);
      if (result.url) {
        // 加个时间戳防止浏览器拿缓存里的旧音频。
        // 同一个任务音频不会变，理论上不需要；但开发期重新生成了 speech.mp3 时，
        // 浏览器会固执地播旧的，加 `?t=` 能省掉"清缓存"这一步。
        setAudioUrl(`${result.url}?t=${Date.now()}`);
      } else {
        // 后端降级时是 200 + ok=false，不是异常。这里要当"功能不可用"提示，
        // 而不是当成任务失败——答案文本还在上面好好显示着。
        setSpeechError(result.note ?? "语音合成失败");
      }
    } catch (e) {
      setSpeechError((e as Error).message);
    } finally {
      setSpeechLoading(false);
    }
  };

  return (
    <section className="panel result">
      <div className="result-head">
        <span className={`pill ${tone}`}>{STATUS_TEXT[meta.status] ?? meta.status}</span>
        <span className="meta-item">步数 {meta.steps}</span>
        <span className="meta-item">
          耗时 {meta.elapsed_s === null ? "—" : `${meta.elapsed_s}s`}
        </span>
        <span className="meta-item mono">{meta.model}</span>

        {/* 播报按钮只在任务结束后出现：跑的过程中没有结论可念，
            后端那边对 running 状态会返回 409，不如前端就别给这个入口。 */}
        {!running && (
          <button className="speak" disabled={speechLoading} onClick={() => void speak()}>
            {speechLoading ? "合成中…" : audioUrl ? "重新合成" : "语音播报"}
          </button>
        )}
      </div>

      {/* P5b：登录态放在答案**之前**。原因：它解释的是"Agent 看到的是哪个版本的
          页面"，读者知道这件事之后才能正确理解下面的答案。
          匿名执行时这个字段不存在，什么都不渲染——那是默认情况，不必特意说。 */}
      {meta.credential && <CredentialBadge credential={meta.credential} />}

      {meta.answer && (
        <div className="answer">
          <span className="label">答案</span>
          <p>{meta.answer}</p>
        </div>
      )}

      {meta.note && <p className="note">说明：{meta.note}</p>}

      {/* 用原生 <audio controls>：播放/暂停/进度/重播全都白送。
          自己写一个播放器要多管 3 个状态，还没法拖进度条。
          autoPlay：用户刚点了按钮，这是一次明确的手势，
          浏览器的自动播放策略允许（没有手势的自动播放会被拦）。 */}
      {audioUrl && (
        <div className="speech">
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <audio controls autoPlay src={audioUrl} />
        </div>
      )}

      {speechError && <p className="note">播报不可用：{speechError}</p>}
    </section>
  );
}
