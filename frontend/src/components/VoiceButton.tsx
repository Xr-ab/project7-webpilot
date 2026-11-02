import { useEffect, useRef, useState } from "react";
import { transcribeAudio } from "../api";

/**
 * 语音输入按钮：按住说话 → 松开转写 → 把文字填进任务目标。
 *
 * 它在项目里的角色：
 * 语音只是**输入的另一种写法**。转写完就退场，后面 Agent 干活走的还是纯文本那条路。
 * 所以这个组件只做一件事：拿到一段音频，换回一段文字，交给父组件去填。
 *
 * 为什么不把录音状态放进 TaskForm：
 * 录音涉及 5 个状态（空闲/申请权限/录音中/上传中/失败）、3 个 ref、1 个定时器、
 * 外加组件卸载时的清理。塞进 TaskForm 会把一个 60 行的表单变成 200 行的怪物，
 * 而且 React 每次 setGoal 都会重渲染表单，跟录音状态搅在一起很难排查。
 */

interface Props {
  disabled: boolean;
  onText: (text: string) => void;
}

/** 录音阶段。用一个字符串枚举而不是 4 个布尔值：4 个布尔能组合出 16 种非法状态。 */
type Phase = "idle" | "starting" | "recording" | "uploading";

// 单次录音上限。超过 30 秒的话：转写变慢、上传变大，而且没人会口述那么长的任务目标。
// 到点自动停并转写，比让用户自己发现"怎么还在录"要好。
const MAX_SECONDS = 30;

/** 当前页面是不是被嵌在别的页面/预览面板里。
 *
 * 为什么要判断这个（真实踩到的第一个坑）：
 * IDE 的内置预览、内嵌浏览器面板都是把页面塞进 iframe 或受限 webview 里。
 * getUserMedia 在 iframe 里需要父页面显式授权（`allow="microphone"`），
 * 没有授权时它会**立刻抛 NotAllowedError，而且不弹权限框**。
 *
 * 后果特别坑：用户看到"没有权限"，跑到浏览器设置里找，也找不到这个站点——
 * 因为它压根没发起过授权请求。用户会以为是代码坏了。
 *
 * 跨域访问 window.top 会抛异常，抛了就说明肯定在 iframe 里（同源才能读）。
 */
function isEmbedded(): boolean {
  try {
    return window.self !== window.top;
  } catch {
    return true;
  }
}

export default function VoiceButton({ disabled, onText }: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState("");

  // 这些必须用 ref 而不是 state：
  // MediaRecorder / MediaStream / 定时器都是"有身份的对象"，
  // 存 state 会触发重渲染，而且闭包里拿到的可能是旧对象。
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number>(0);
  /**
   * "用户已经松手了，但录音还没真正开始"的标记。
   *
   * 为什么需要：按下 → getUserMedia 要等权限/设备初始化（几十到几百毫秒）。
   * 如果用户手快，在这期间就松手了，此时 recorderRef 还是 null，
   * stopRecording() 找不到东西可停，只能空转返回——然后录音才刚开始，
   * 一路录到 30 秒上限。用户看到的是"我就点了一下，它自己录了半分钟"。
   * 有了这个标记：松手时先记下来，等录音真开始了立刻补一次 stop。
   */
  const pendingStopRef = useRef(false);

  /** 卸下麦克风。**这一步绝不能忘**，否则浏览器标签页上的红点会一直亮着，
   *  用户会以为在偷偷录音——这是最容易被忽略、也最影响信任的一个细节。 */
  const releaseStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  const clearTimer = () => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = 0;
    }
  };

  /** 组件卸载时善后：录音中直接切走页面，也要把麦克风和定时器关掉。 */
  useEffect(() => {
    return () => {
      clearTimer();
      if (recorderRef.current?.state === "recording") {
        recorderRef.current.stop();
      }
      releaseStream();
    };
  }, []);

  const startRecording = async () => {
    setError("");

    // 嵌在预览面板里就别试了：试也是立刻 NotAllowedError，还容易让人以为程序坏了。
    // 直接告诉用户"换个入口"，这是唯一有效的解决办法。
    if (isEmbedded()) {
      setError(
        "当前页面被嵌在预览窗口里，嵌入页面拿不到麦克风权限（且不会弹授权框）。" +
          "请用 Edge / Chrome 直接打开 http://localhost:5173",
      );
      return;
    }

    // 非 HTTPS 且非 localhost 时，浏览器根本不给 mediaDevices（安全上下文限制）。
    // 本地开发是 localhost 所以没问题；部署到 http://内网IP 就会踩到这个坑。
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("当前环境不支持录音（需要 HTTPS 或 localhost）");
      return;
    }
    if (typeof MediaRecorder === "undefined") {
      setError("当前浏览器不支持录音");
      return;
    }

    setPhase("starting");
    pendingStopRef.current = false; // 每次开始都先清掉上一轮的标记
    try {
      // getUserMedia 会弹权限框。用户点"拒绝"时这里抛 NotAllowedError，
      // 必须捕获——不捕获就是一个未处理的 Promise 拒绝，控制台红一片，界面却毫无提示。
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];

      // 不指定 mimeType，让浏览器自己挑最合适的：
      // Chrome 给 audio/webm;codecs=opus，Safari 给 audio/mp4。
      // 手写死 webm 在 Safari 上会直接构造失败。解码兼容性交给后端 av 处理。
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;

      // timeslice=250：每 250ms 吐一个数据块。
      // 不传的话只在 stop 时一次性给出全部数据；万一中途异常，
      // 已经说的话就全丢了。分块拿至少能保住前半段。
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };

      recorder.onstop = () => {
        clearTimer();
        releaseStream();

        // 用 recorder.mimeType 而不是写死 "audio/webm"：
        // Safari 产的是 mp4，写死 webm 后后端会当成坏文件。
        const type = recorder.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        chunksRef.current = [];

        if (blob.size < 1024) {
          // 太短基本是误触（点一下就松手），不值得跑一次模型
          setPhase("idle");
          setError("录音太短，再说一次");
          return;
        }
        void upload(blob);
      };

      recorder.start(250);
      setSeconds(0);
      setPhase("recording");

      // 补一次 stop：用户在"申请麦克风"阶段就松手了（见 pendingStopRef 的注释）。
      // 延 120ms 是为了让 MediaRecorder 至少收到一个 chunk，
      // 否则 blob 会是 0 字节，连"太短"都判不出来，直接变成空上传。
      if (pendingStopRef.current) {
        pendingStopRef.current = false;
        window.setTimeout(() => {
          if (recorderRef.current?.state === "recording") recorderRef.current.stop();
        }, 120);
      }

      // 计时器：既用于显示已录秒数，也负责到点自动停
      timerRef.current = window.setInterval(() => {
        setSeconds((prev) => {
          const next = prev + 1;
          if (next >= MAX_SECONDS) {
            // 这里不能直接调 stopRecording()（它依赖外部 state），
            // 直接操作 ref 上的 recorder 最稳
            if (recorderRef.current?.state === "recording") {
              recorderRef.current.stop();
            }
          }
          return next;
        });
      }, 1000);
    } catch (e) {
      releaseStream();
      setPhase("idle");
      const name = (e as Error).name;
      // 把浏览器的英文错误名**保留在提示里**（方括号中）。
      // 只显示中文翻译的话，出现"没权限"时根本分不清是：
      //   用户点了拒绝 / 系统禁用 / 嵌入页面无授权 / 非安全上下文
      // 这四种的原因和修法完全不同。留个错误名，看一眼就知道该往哪查。
      if (name === "NotAllowedError") {
        setError(
          "麦克风被拒绝 [NotAllowedError]。点地址栏左侧图标 → 麦克风 → 允许；" +
            "若那里根本没有本站，说明是被浏览器或嵌入环境拦掉的。",
        );
      } else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        setError("没检测到麦克风设备 [NotFoundError]");
      } else if (name === "NotReadableError") {
        setError("麦克风被其他程序占用 [NotReadableError]，关掉会议/录音软件再试");
      } else if (name === "SecurityError") {
        setError("非安全上下文 [SecurityError]，需要 HTTPS 或用 localhost 访问");
      } else {
        setError(`录音失败 [${name}]：${(e as Error).message}`);
      }
    }
  };

  const stopRecording = () => {
    // 还在等权限/初始化：记下来，等真开始了再补停（见 pendingStopRef）
    if (phase === "starting") {
      pendingStopRef.current = true;
      return;
    }
    if (recorderRef.current?.state === "recording") {
      recorderRef.current.stop(); // 触发 onstop，后面流程都在那儿
    }
  };

  const upload = async (blob: Blob) => {
    setPhase("uploading");
    try {
      const result = await transcribeAudio(blob);
      if (result.text) {
        onText(result.text);
        setError(""); // 成功要清掉上次的报错，否则"录音太短"会一直挂在界面上
      } else {
        setError(result.note ?? "没听清，再说一次");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPhase("idle");
      setSeconds(0);
    }
  };

  // ---- 渲染 ----
  const recording = phase === "recording";
  const busy = phase === "starting" || phase === "uploading";

  const label = () => {
    if (phase === "starting") return "申请麦克风…";
    if (phase === "uploading") return "识别中…";
    if (recording) return `松手结束 ${MAX_SECONDS - seconds}s`;
    return "语音输入";
  };

  // 用 onMouseDown/onMouseUp 实现"按住说话"，
  // 而不是 onClick 切换：按住说话更符合对讲机的心智，也少一个"我到底在没在录"的判断。
  // onMouseLeave 处理"按住后移出按钮"——不处理的话手滑出去就再也停不下来。
  return (
    <div className="voice">
      <button
        type="button"
        className={`mic ${recording ? "on" : ""}`}
        disabled={disabled || busy}
        onMouseDown={() => void startRecording()}
        onMouseUp={stopRecording}
        // 不写成 `recording && stopRecording()`：那样在"申请麦克风"阶段移出按钮就停不下来了。
        // stopRecording 自己会判断当前该不该停，调用方不用管。
        onMouseLeave={stopRecording}
        // 触屏设备没有 mouse 事件，补一套 touch
        onTouchStart={(e) => {
          e.preventDefault(); // 阻止长按弹系统菜单
          void startRecording();
        }}
        onTouchEnd={(e) => {
          e.preventDefault();
          stopRecording();
        }}
      >
        <span className="dot" />
        {label()}
      </button>
      {recording && <span className="rec-hint">正在录音，说完松手</span>}
      {/* 嵌入式环境下提前告知，别等用户点了才发现不能用 */}
      {isEmbedded() && (
        <p className="voice-warn">
          预览窗口里麦克风不可用，请用 Edge / Chrome 打开 http://localhost:5173
        </p>
      )}
      {error && <p className="voice-error">{error}</p>}
    </div>
  );
}
