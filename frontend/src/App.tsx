import { useEffect, useRef, useState } from "react";
import ApprovalCard from "./components/ApprovalCard";
import AskPanel from "./components/AskPanel";
import AuthPanel from "./components/AuthPanel";
import CredentialPanel from "./components/CredentialPanel";
import MyTasks from "./components/MyTasks";
import ResultPanel from "./components/ResultPanel";
import StatsPanel from "./components/StatsPanel";
import TaskForm from "./components/TaskForm";
import VoiceSession from "./components/VoiceSession";
import TraceTimeline from "./components/TraceTimeline";
import { clearAuth, fetchKnowledgeTask, fetchTask, fetchTrace, getEmail, isLoggedIn, submitTask } from "./api";
import type { TaskMeta, TraceItem } from "./types";

export default function App() {
  // M4 认证：没 token 就只渲染登录卡片。
  // 用 state 而不是路由：这个应用只有"登录"和"主界面"两个形态，
  // 为两个页面引 react-router 不划算。
  const [loggedIn, setLoggedIn] = useState(isLoggedIn());

  const [taskId, setTaskId] = useState<string | null>(null);
  // 要轮询的任务 id（只有"自己提交的任务"才轮询）。
  // 为什么要和 taskId 分开：从问答来源打开的是别人的历史任务，已经是终态，
  // 既不需要轮询、也不能走需要归属校验的 /tasks/{id} 接口（会 404）。
  const [pollTaskId, setPollTaskId] = useState<string | null>(null);
  // 每次"要求加载某个任务"就 +1，作为轮询 effect 的额外依赖。
  // 为什么需要它（实测踩到的 bug）：点"我的任务"里**正在展示的那条**时，
  // pollTaskId 的新旧值相同 → setState 不触发重渲染 → effect 不重跑 →
  // 而 openOwnTask 已经把 meta 清空了，右列就停在"还没有任务"再也回不来。
  // 加一个自增 nonce 后，"同一个 id 再点一次"也能真正重新拉一遍。
  const [pollNonce, setPollNonce] = useState(0);
  // 上一次看到的任务状态。用它做到"**状态每变一次**才刷新左列列表"，
  // 而不是每次轮询都刷——P5a-4 之后待审批期间会一直轮询，
  // 不判断的话「我的任务」会被带着每 3 秒打一次 DB，而那个列表本身是刻意不自动轮询的。
  const lastStatus = useRef("");
  const [meta, setMeta] = useState<TaskMeta | null>(null);
  const [trace, setTrace] = useState<TraceItem[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // "我的任务"列表的刷新信号：任务提交后 +1，MyTasks 内部会重新拉一次。
  // 用一个自增数字而不是传回调：组件只关心"要不要刷"，不关心"谁让它刷的"。
  const [tasksRefresh, setTasksRefresh] = useState(0);
  // P5b：登录态列表的刷新信号。保存/删除后 +1，TaskForm 的下拉重新拉一次。
  // 和 tasksRefresh 同一个套路——跨组件"通知"用自增数字最不容易出循环。
  const [credRefresh, setCredRefresh] = useState(0);

  const run = async (goal: string, startUrl: string, credentialId: number | null) => {
    setError("");
    setTrace([]);
    setMeta(null);
    // 换任务了：重置"上次状态"，否则新任务的第一步不会被当成状态变化、左列不刷
    lastStatus.current = "";
    setBusy(true);
    try {
      const id = await submitTask(goal, startUrl, credentialId);
      setTaskId(id);
      setPollTaskId(id);
      setPollNonce((n) => n + 1);
      // 列表里立刻出现这条新任务（不然要手动点刷新才看得到）
      setTasksRefresh((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
      // token 过期时 authFetch 会清本地 token 并抛"登录已失效"，这里回到登录页
      if (!isLoggedIn()) setLoggedIn(false);
    }
  };

  // 轮询：v0.1 用 setTimeout 自循环而不用 setInterval，
  // 因为"下一次什么时候拉"应该由这一次的结果决定——任务结束了就不再拉，
  // setInterval 会一直空转，页面关了还可能在拉。
  // 依赖 pollTaskId 而不是 taskId：从问答来源打开历史任务时不进入轮询。
  useEffect(() => {
    if (!pollTaskId) return;

    let stopped = false;
    let timer = 0;

    const tick = async () => {
      try {
        const [nextMeta, nextTrace] = await Promise.all([
          fetchTask(pollTaskId),
          fetchTrace(pollTaskId),
        ]);
        if (stopped) return;
        setMeta(nextMeta);
        setTrace(nextTrace);

        // 状态变了才刷左列（见 lastStatus 的注释）。放在分支之前：
        // 从 running 掉进 waiting_approval 这一刻也要刷，否则列表一直显示"执行中"
        if (lastStatus.current !== nextMeta.status) {
          lastStatus.current = nextMeta.status;
          setTasksRefresh((n) => n + 1);
        }

        if (nextMeta.status === "queued" || nextMeta.status === "running") {
          timer = window.setTimeout(tick, 1000);
        } else if (nextMeta.status === "waiting_approval") {
          // P5a-4：待审批既不是"跑完了"（不能停轮询），也不占 worker（不该 1 秒连打）。
          // 停下来是最省事的写法，但那样批准之后界面永远停在"待审批"——
          // 得靠"提交审批后手动重启轮询"这种补丁续命，两处状态要同步，是 bug 温床。
          // 所以继续轮，只是把间隔拉到 3 秒：看得出状态变化，又不会在等人做决定的
          // 几分钟里打上百次 DB。
          setBusy(false);
          timer = window.setTimeout(tick, 3000);
        } else {
          setBusy(false);
        }
      } catch (e) {
        if (stopped) return;
        setError((e as Error).message);
        setBusy(false);
        if (!isLoggedIn()) setLoggedIn(false);
      }
    };

    tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
    // pollNonce：同一个 task_id 被再次要求加载时也能重跑（见上方注释）
  }, [pollTaskId, pollNonce]);

  // 从问答来源点回原始任务：走只读的 /knowledge/tasks/{id}（不做归属校验，
  // 但要求任务确实被知识库收录），一次性拉完即止，不进入轮询。
  const openTask = async (id: string) => {
    setError("");
    setTrace([]);
    setMeta(null);
    setBusy(false);
    setTaskId(id);
    setPollTaskId(null); // 来源任务是终态的，不轮询
    try {
      const data = await fetchKnowledgeTask(id);
      setMeta(data.meta);
      setTrace(data.trace);
    } catch (e) {
      setError((e as Error).message);
    }
    // 右列在下方，不滚动的话用户看不到加载结果（左列操作、右列结果）
    requestAnimationFrame(() => {
      document.getElementById("main-col")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };

  // 点"我的任务"里的一条：这是自己的任务，走正常的 /tasks/{id} + 轮询。
  // 已经终态的任务，第一次 tick 拉完就停（不会在后台空转）；
  // 还在跑的任务会自然进入轮询，和刚提交完的体验一致。
  const openOwnTask = (id: string) => {
    setError("");
    setTrace([]);
    setMeta(null);
    lastStatus.current = "";
    setTaskId(id);
    setPollTaskId(id);
    setPollNonce((n) => n + 1);
  };

  // 审批卡片提交成功后：立刻重拉一次任务。
  // 不这么做的话，POST 返回时本地 meta 还停在 waiting_approval，
  // 要等下一次轮询（最长 3 秒）才会变——卡片会白挂一会儿，用户以为没点中。
  const onApprovalDecided = () => {
    setPollNonce((n) => n + 1);
    setTasksRefresh((n) => n + 1);
  };

  // 未登录：整个页面就是登录卡片（顶栏也不渲染，避免出现"半可用"的界面）
  if (!loggedIn) {
    return <AuthPanel onDone={() => setLoggedIn(true)} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>WebPilot</h1>
          <p className="sub">浏览器操作 Agent · 本地闭环 v0.1</p>
        </div>
        <nav className="links">
          <a href="/testsite/index.html" target="_blank" rel="noreferrer">
            测试站
          </a>
          <a href="/docs" target="_blank" rel="noreferrer">
            接口文档
          </a>
          {/* 当前登录账号 + 退出。把"我是谁"显示出来，才不会混淆数据归属 */}
          <span className="whoami" title="当前登录账号">
            {getEmail()}
          </span>
          <button
            className="link-btn"
            onClick={() => {
              clearAuth();
              setLoggedIn(false);
              setTaskId(null);
              setMeta(null);
              setTrace([]);
            }}
          >
            退出
          </button>
        </nav>
      </header>

      <main className="layout">
        <div className="side-col">
          {/* M4 收尾：我的任务列表。放最上面——
              它是"我有哪些东西"的导航，逻辑上在"我要做什么"之前。
              openOwnTask 走带归属校验的正门（和问答来源的只读通道不同）。 */}
          <MyTasks onOpen={openOwnTask} refreshKey={tasksRefresh} />
          {/* P5b：登录态管理。放在"我的任务"和"任务表单"之间——
              它是下面那个下拉的数据来源，视线顺序上"先有身份，再发任务"。
              默认收起，只占一行，不跟主流程抢位置。 */}
          <CredentialPanel onChanged={() => setCredRefresh((n) => n + 1)} />
          <TaskForm
            running={busy}
            error={error}
            onRun={run}
            credRefresh={credRefresh}
          />
          {/* P6b：语音会话面板。紧挨任务表单——
              两者都是"我要让它干什么"的入口，只是表达方式不同（说 vs 写）。
              不替换 VoiceButton：那个是"语音转文字填进表单"，流程更短；
              这个面板是"会话"，说完自动跑任务并播报，两者场景不冲突。 */}
          <VoiceSession />
          {/* P4：知识库问答。放左列（和"提交任务"同为操作入口），
              右列保持"任务执行结果"的语义。
              onOpenTask：点来源 → 右列加载那个任务的结论与轨迹（溯源闭环） */}
          <AskPanel onOpenTask={openTask} />
        </div>
        <div className="main-col" id="main-col">
          <ResultPanel meta={meta} taskId={taskId} />
          {/* P5a-4：待审批卡片。放在结论下面而不是最顶上——
              结论区的状态胶囊已经在说"待审批"，人要先知道"这是什么任务、跑到哪了"，
              再看"现在要我决定什么"。它是插入式的：不在等待时渲染 null。 */}
          <ApprovalCard
            taskId={pollTaskId ?? ""}
            active={meta?.status === "waiting_approval"}
            onDecided={onApprovalDecided}
          />
          <TraceTimeline items={trace} running={busy} />
          {/* M5c：只读看板。放在主列最底下——主流程（跑任务看轨迹）才是页面主角，
              统计是"跑完评测回来对账"的场景，不需要抢首屏位置 */}
          <StatsPanel />
        </div>
      </main>
    </div>
  );
}
