import { useCallback, useEffect, useRef, useState } from "react";
import { getToken } from "../api";
import { VoiceSocket, type SessionStatus, type VoiceEvent } from "../ws";

/**
 * 语音会话面板（P6b-B5）：一条 WS 长连接里连着说多轮。
 *
 * 它和 VoiceButton 的区别（为什么要两个组件而不是改一个）：
 * - VoiceButton 是"输入的另一种写法"：录一句 → 转成文字 → 填进表单，然后退场。
 * - 这个面板是**会话**：连接常驻，说完就自动跑任务、实时推进度、念结论，
 *   还能在播报时打断它。两者的生命周期和状态完全不同，硬合并只会互相拖累。
 *
 * 它和现有 TaskForm/轨迹区的关系：独立面板，不抢主流程。
 * 语音触发出来的任务照样走完整后端链路（审批、隔离、统计），
 * 只是"提交方式"换成了说一句话。
 *
 * 三条交互纪律（写在这里，因为它们是产品决定，不是实现细节）：
 * 1. 录音期间不显示"提交任务"入口——避免一句话触发两个任务；
 * 2. 播报中说话 = barge-in：先本地暂停播放（体感立即），同时通知服务端丢掉后续块；
 * 3. 断线后自动重连 3 次，失败就明说"语音不可用，请用文本"——语音是增强项。
 *
 * ⚠️ 诚实的技术边界：播报是"伪流式"。
 * 服务端确实按块推 mp3（tts.chunk），但浏览器端要等 tts.end 才把块拼成 Blob 播放——
 * 真正"首包即播"需要 MediaSource + audio/mpeg（Safari 不支持）或换成 Opus 流。
 * 当前答案通常在 2~4 秒音频，整体播放的延迟差可以接受；
 * 逐块播放列进 backlog（和"真流式 ASR"同一条：换引擎/换编解码，不在本阶段）。
 */

interface Line {
  role: "you" | "agent" | "sys";
  text: string;
}

type Connecting = SessionStatus;

/** 单句录音上限：和 VoiceButton 一致（说任务目标不会超过 20 秒） */
const MAX_SECONDS = 20;

export default function VoiceSession() {
  const [status, setStatus] = useState<Connecting>("connecting");
  const [statusDetail, setStatusDetail] = useState("");
  const [lines, setLines] = useState<Line[]>([]);
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState("");

  const socketRef = useRef<VoiceSocket | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number>(0);
  // 播报缓冲：tts.chunk 的 base64 片段。用 ref 不用 state——
  // 它是"攒够了一口气拼出来"的中间产物，不需要触发渲染
  const audioPartsRef = useRef<Blob[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const push = useCallback((role: Line["role"], text: string) => {
    setLines((prev) => [...prev.slice(-40), { role, text }]); // 只留最近 40 条，页面不无限长
  }, []);

  // ---- 服务端事件分发 ----
  const onEvent = useCallback(
    (event: VoiceEvent) => {
      switch (event.type) {
        case "asr.final": {
          const text = String(event.text ?? "");
          if (text) push("you", text);
          break;
        }
        case "task.start":
          push("agent", `开始执行：${String(event.goal ?? "")}`);
          break;
        case "task.step":
          // 每一步都显示会让面板很快被刷满，只显示"第 N 步做了啥"
          push("agent", `第 ${event.step} 步：${String(event.action ?? "")}`);
          break;
        case "task.final": {
          const answer = String(event.answer ?? "");
          const st = String(event.status ?? "");
          if (answer) push("agent", answer);
          else push("sys", `任务结束（${st}）`);
          break;
        }
        case "tts.chunk": {
          const data = String(event.data ?? "");
          if (!data) break;
          // base64 → 二进制：先把字符串解成字节，再包成 Blob 片段
          const bin = atob(data);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
          audioPartsRef.current.push(new Blob([bytes], { type: "audio/mpeg" }));
          break;
        }
        case "tts.end": {
          const interrupted = event.interrupted === true;
          const parts = audioPartsRef.current;
          audioPartsRef.current = [];
          if (interrupted || parts.length === 0) break;
          // 拼成一段 mp3 播放（见文件头"诚实的技术边界"）
          const url = URL.createObjectURL(new Blob(parts, { type: "audio/mpeg" }));
          const audio = new Audio(url);
          audioRef.current = audio;
          setSpeaking(true);
          audio.onended = () => {
            setSpeaking(false);
            URL.revokeObjectURL(url); // 不释放会一直占着内存
          };
          void audio.play().catch(() => setSpeaking(false));
          break;
        }
        case "error": {
          const code = String(event.code ?? "");
          const message = String(event.message ?? "");
          if (code === "empty_text") push("sys", "没听清，请再说一次");
          else if (code === "too_short") push("sys", "录音太短，再说一次");
          else push("sys", `出错了：${message}`);
          break;
        }
        default:
          break;
      }
    },
    [push],
  );

  // ---- 连接管理 ----
  useEffect(() => {
    const token = getToken();
    if (!token) {
      setStatus("failed");
      setStatusDetail("未登录，语音会话不可用");
      return;
    }
    const socket = new VoiceSocket(token, {
      onEvent,
      onStatus: (s, detail) => {
        setStatus(s);
        setStatusDetail(detail ?? "");
      },
    });
    socketRef.current = socket;
    socket.connect();
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [onEvent]);

  /** 打断播报：本地立刻停（体感）+ 告知服务端丢块（省流量） */
  const bargeIn = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setSpeaking(false);
    socketRef.current?.send({ type: "interrupt" });
  }, []);

  // ---- 录音 ----
  const releaseStream = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  };

  const clearTimer = () => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = 0;
    }
  };

  useEffect(() => () => {
    clearTimer();
    releaseStream();
    audioRef.current?.pause();
  }, []);

  const startRecording = async () => {
    setError("");
    if (!socketRef.current?.isReady) {
      setError("语音通道还没连上，稍等一下或改用文本提交");
      return;
    }
    // 播报中开口 = 打断（服务端也会因为收到 audio.append 自行打断）
    if (speaking) bargeIn();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        clearTimer();
        releaseStream();
        setRecording(false);
        setSeconds(0);
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        chunksRef.current = [];
        if (blob.size < 1024) {
          push("sys", "录音太短，再说一次");
          return;
        }
        void sendAudio(blob);
      };

      recorder.start(250);
      setRecording(true);
      setSeconds(0);
      timerRef.current = window.setInterval(() => {
        setSeconds((prev) => {
          const next = prev + 1;
          if (next >= MAX_SECONDS && recorderRef.current?.state === "recording") {
            recorderRef.current.stop();
          }
          return next;
        });
      }, 1000);
    } catch (e) {
      releaseStream();
      setRecording(false);
      const name = (e as Error).name;
      if (name === "NotAllowedError") {
        setError("麦克风被拒绝 [NotAllowedError]：点地址栏左侧图标 → 麦克风 → 允许");
      } else {
        setError(`录音失败 [${name}]：${(e as Error).message}`);
      }
    }
  };

  const stopRecording = () => {
    if (recorderRef.current?.state === "recording") recorderRef.current.stop();
  };

  /** 把一段音频切成 16KB 的块推上去（协议要求 seq + final）。 */
  const sendAudio = async (blob: Blob) => {
    const buffer = await blob.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    const CHUNK = 16 * 1024;
    let seq = 0;
    for (let start = 0; start < bytes.length; start += CHUNK) {
      const piece = bytes.slice(start, start + CHUNK);
      const final = start + CHUNK >= bytes.length;
      // base64：btoa 只吃 latin1，先逐字节转成字符串
      let bin = "";
      piece.forEach((b) => {
        bin += String.fromCharCode(b);
      });
      const ok = socketRef.current?.send({
        type: "audio.append",
        seq,
        data: btoa(bin),
        final,
      });
      if (!ok) {
        setError("连接已断开，这一句没有发出去");
        return;
      }
      seq += 1;
    }
  };

  const badge = () => {
    if (status === "ready") return "● 已连接";
    if (status === "connecting") return "○ 连接中…";
    if (status === "reconnecting") return "◐ 重连中…";
    if (status === "failed") return "✕ 不可用";
    return "○ 已关闭";
  };

  return (
    <section className="panel voice-session">
      <header className="vs-head">
        <h2>语音会话</h2>
        <span className={`vs-badge ${status}`}>{badge()}</span>
      </header>

      {statusDetail && <p className="vs-hint">{statusDetail}</p>}

      <div className="vs-log">
        {lines.length === 0 && (
          <p className="vs-empty">按住下面的麦克风说一句话，它会听完就去干活，并把结果念给你听。</p>
        )}
        {lines.map((line, i) => (
          <p key={i} className={`vs-line ${line.role}`}>
            <span className="vs-who">
              {line.role === "you" ? "你" : line.role === "agent" ? "Agent" : "系统"}
            </span>
            {line.text}
          </p>
        ))}
      </div>

      {speaking && (
        <button type="button" className="vs-barge" onClick={bargeIn}>
          🔊 正在播报，点这里或直接说话即可打断
        </button>
      )}

      <div className="vs-controls">
        <button
          type="button"
          className={`vs-mic ${recording ? "on" : ""}`}
          disabled={status !== "ready" && !recording}
          onMouseDown={() => void startRecording()}
          onMouseUp={stopRecording}
          onMouseLeave={stopRecording}
          onTouchStart={(e) => {
            e.preventDefault();
            void startRecording();
          }}
          onTouchEnd={(e) => {
            e.preventDefault();
            stopRecording();
          }}
        >
          {recording ? `松手结束 ${MAX_SECONDS - seconds}s` : "🎤 按住说话"}
        </button>
        {recording && <span className="vs-hint">正在录音，说完松手</span>}
      </div>

      {error && <p className="vs-error">{error}</p>}
    </section>
  );
}
