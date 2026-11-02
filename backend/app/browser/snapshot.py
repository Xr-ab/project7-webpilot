"""页面状态提取：把"一个网页"翻译成"一段模型能读的文本 + 一张可点的元素清单"。

角色：Agent 四步循环里的第 1 步 observe。
- session.py（M2）负责"手"：点、输、滚、跳
- snapshot.py（M3）负责"眼"：看清楚现在页面上有什么、能点什么
- workflow.py（M4）负责"脑子"：拿这份快照问模型下一步干什么

为什么不让模型直接看 HTML：
1. 一个真实页面 HTML 动辄几十万字符，塞不进上下文，也贵
2. HTML 里 90% 是 <div>/<style>/<script> 噪音，模型容易被无关结构带偏
3. HTML 里的元素没法直接"指"——模型只能说"点第 3 个链接"，得有个稳定的指代方式

所以这里的核心设计是「编号」：
- 用 JS 遍历页面上所有可交互元素，给每个打上 data-wp-id="e1" / "e2" ... 的 DOM 属性
- 清单里只写 e1/e2，模型回答时也只写 e1
- 动作层用 [data-wp-id="e1"] 这个选择器去点 —— 一定点得到，不靠猜 CSS 选择器

⚠️ 编号的时效性（这是浏览器 Agent 最容易翻车的地方）：
编号只对"当轮快照"有效。页面一刷新、一跳转、一发生列表重排，DOM 变了，旧编号就过期了。
所以每次 take_snapshot 都会先清掉上一轮的属性再重新编号；
动作执行失败（找不到元素）时的正确做法是「重新快照」而不是「重试同一个编号」。
"""
from __future__ import annotations

import re
from typing import Any, TypedDict

from playwright.async_api import Page

# 单次快照最多列多少个元素。
# 为什么要有上限：元素清单本身就是上下文成本，一个列表页很容易有几百个 <a>。
# 80 是经验值——够覆盖一般页面的主要操作区，又不至于把 prompt 撑爆。
MAX_ELEMENTS = 80

# 正文最多保留多少字符。超了就截断并在快照里标记 truncated=True。
# 为什么不干脆不放正文：很多任务的答案（价格、库存、"共 N 条结果"）就在正文里，
# 模型不看正文就只能靠猜。
#
# 为什么从 1200 提到 3000（真实站点逼出来的）：
# quotes page/2 的正文有 3445 字符，而答案「第一条名言的作者」那一句
# `by Marilyn Monroe` 落在**第 1108 字符**——1200 的预算按 60/40 切开后，
# 头部只有 720 字符，正好把作者名切在外面。于是模型在头部看不到答案，
# 转头把尾部（从第 2965 字符开始）里第 9 条引言的 `by Mark Twain` 当成了第一条。
# 也就是说：这里不是"截断丢信息"，而是"截断位置恰好切断了唯一答案"，害处远大于省下的 token。
#
# 3000 + 1.25 容差 = 3750，常见的列表页/详情页整段放得下，不再触发拼接。
# 真正巨大的页面（几万字符的文档站）仍然会截断，那是对的。
MAX_TEXT_CHARS = 3000

# 编号属性的名字。加 wp- 前缀避免和页面自己的 data-* 撞车。
ID_ATTR = "data-wp-id"

# 元素编号的合法格式：e + 正整数。用来校验模型返回的 id，防止有人拿它拼选择器搞注入。
_ID_PATTERN = re.compile(r"^e\d{1,3}$")


class SnapshotElement(TypedDict, total=False):
    """清单里的一项。字段按需出现，没值的键不会写进去（省 token）。"""

    id: str  # e3，本轮有效
    role: str  # link / button / input / select / checkbox ...
    text: str  # 给模型看的名字：链接文字、按钮文字、字段名
    href: str  # 只有 <a> 才有
    value: str  # 输入框当前内容（密码打码）
    checked: bool  # checkbox / radio
    selected_text: str  # <select> 当前选中项的文字
    in_viewport: bool  # 是否在可视区域内（不在的要么滚动，要么看不见）
    # 输入框的 type（text / password / email / number ...）。
    # ⚠️ 只给审批闸门用，不进 render_snapshot_text —— 不能让模型看见，
    # 否则 prompt 一变，M5a 回放器拿历史快照（没有这个字段）重放就会出现系统性差异。
    input_type: str
    # 元素包围盒，归一化到视口：{x, y, w, h}，各值 0~1（元素在视口外时可为负或 >1）。
    # P6 视觉兜底的「元素↔像素」桥：截图是视口的像素图，bbox 乘上截图尺寸
    # 就是像素位置——视觉模型说的「图中左上角的按钮」才能对回 e3 这样的编号。
    # 为什么归一化而不是存像素：截图尺寸随视口变，比值不变，
    # 同一份 trace 换个分辨率回放仍然可解释。
    # ⚠️ 只落盘给视觉模块用，不进 render_snapshot_text —— 纪律同 input_type（回放基线）。
    bbox: dict[str, float]


class PageSnapshot(TypedDict):
    """一份完整的页面快照。可以直接 json.dumps 落盘。"""

    url: str
    title: str
    headings: list[str]
    text: str
    text_truncated: bool
    elements: list[SnapshotElement]
    # 当前有弹窗/对话框打开着。True 时 elements 只包含弹窗里的元素——
    # 因为弹窗外的东西此刻确实点不到（Playwright 会报 intercepts pointer events）
    modal_open: bool
    elements_total: int
    elements_shown: int
    scroll: dict[str, int]


# ---------------------------------------------------------------------------
# 注入到页面里执行的 JS。
# 写成一段字符串常量而不是嵌在函数里，方便单独调试（可以直接贴到浏览器控制台跑）。
# 参数通过 page.evaluate 的第二个参数传进去，不要用 f-string 拼——拼字符串会被引号坑死。
# ---------------------------------------------------------------------------
_SNAPSHOT_JS = """
({ maxElements, maxText }) => {
  const ATTR = 'data-wp-id';

  // ---- 0. 收集所有可穿透的根：document + 各层 open shadowRoot ----
  // 为什么要做：Web Component 内部的控件不在 document 里，querySelectorAll 找不到。
  // 真实站点（MDN 首页）的搜索入口 <mdn-homepage-search> 和搜索弹窗 <mdn-search-modal>
  // 都在 shadow DOM 里 —— 只走 light DOM 时，模型看到的元素清单里根本没有「搜索」这个东西，
  // 任务从第一步就不可能完成（R4 实测：它只能拿 button 当输入框，报「不能往 button 里输入」）。
  // 只穿 open：closed 的 shadowRoot 规范上就读不到，没办法。
  // 深度限 3：正常组件不会嵌得更深，限深是为了避免极端页面把这里变成 O(深×宽) 的遍历。
  const roots = [document];
  const collectRoots = (root, depth) => {
    if (depth >= 3) return;
    root.querySelectorAll('*').forEach((el) => {
      if (!el.shadowRoot) return;
      roots.push(el.shadowRoot);
      collectRoots(el.shadowRoot, depth + 1);
    });
  };
  collectRoots(document, 0);

  // ---- 0.1 清掉上一轮的编号 ----
  // 不清的话，上一轮存在、这一轮消失的元素还带着旧属性，会查到幽灵元素。
  // 编号现在可能落在 shadow DOM 里，所以清理也要遍历所有根。
  roots.forEach((root) => {
    root.querySelectorAll('[' + ATTR + ']').forEach((el) => el.removeAttribute(ATTR));
  });

  // ---- 1. 哪些元素算"可交互" ----
  // 只枚举真正能操作的东西。div/span 这些就算绑了事件也不列，
  // 否则清单会被布局容器淹没。onclick / role 这两个是兜底：很多站点用 div 当按钮。
  const SELECTOR = [
    'a[href]',
    'button',
    'input:not([type=hidden])',
    'select',
    'textarea',
    '[role=button]',
    '[role=link]',
    '[role=tab]',
    '[role=checkbox]',
    '[onclick]',
    'summary'
  ].join(',');

  // ---- 2. 可见性判断 ----
  // 页面上常见"不可见但存在"的元素（隐藏的下拉菜单、display:none 的提示条、
  // 只给屏幕阅读器看的占位符）。把它列给模型 = 让它去点一个点不到的东西。
  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (Number(style.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const inViewport = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight &&
           r.right > 0 && r.left < window.innerWidth;
  };

  // 压掉换行和连续空格：HTML 里的缩进对模型毫无意义，只会浪费 token
  const clean = (s, n) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, n);

  // ---- 3. 判断"这是什么" ----
  // 优先用站点自己声明的 role，没有就按标签推断
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button') return 'button';
      return 'input';
    }
    return tag;
  };

  // ---- 4. 给元素起个"人话名字" ----
  // 模型靠这个名字决定点不点它，所以名字取得准不准直接决定任务成败。
  // 顺序：aria-label > <label> 关联文字 > 元素自身文字 > 兜底属性
  //
  // 取 <label> 文字必须小心：<label>城市 <select>…</select></label> 的 innerText
  // 会把下拉框里所有选项的文字都算进来（"城市 请选择 福州 厦门 杭州"）。
  // 所以要克隆一份、把表单控件删掉再取文字。
  const labelTextOf = (labelEl) => {
    if (!labelEl) return '';
    const clone = labelEl.cloneNode(true);
    clone.querySelectorAll('input,select,textarea,button').forEach((n) => n.remove());
    return clean(clone.textContent, 60);
  };

  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return clean(aria, 60);

    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      // 表单字段自己没文字，名字在旁边的 <label> 里
      if (el.id) {
        const lb = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lb) {
          const t = labelTextOf(lb);
          if (t) return t;
        }
      }
      const t = labelTextOf(el.closest('label'));
      if (t) return t;
    }

    const own = el.innerText || el.value || el.getAttribute('placeholder') ||
                el.getAttribute('title') || el.getAttribute('alt') || '';
    return clean(own, 60);
  };

  // ---- 5. 带上"当前状态" ----
  // 为什么重要：Agent 需要知道"这个框我已经填过了""这个下拉现在是福州"，
  // 否则它会反复填同一个框（死循环的头号来源）
  const stateOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      // input_type 只服务审批闸门（判断"这一步是不是在填密码"），
      // **不进 render_snapshot_text**——不给模型看。
      // 为什么要加：password 的 value 被打码成 ***，从快照上和普通文本框毫无区别，
      // 闸门就失去了唯一可靠的判断依据（靠字段文案猜"密码"迟早漏）。
      const kind = { input_type: type };
      if (type === 'checkbox' || type === 'radio') return Object.assign({ checked: el.checked }, kind);
      if (type === 'password') return Object.assign({ value: el.value ? '***' : '' }, kind);
      return Object.assign({ value: clean(el.value, 80) }, kind);
    }
    if (tag === 'select') {
      const opt = el.selectedOptions && el.selectedOptions[0];
      return { value: el.value, selected_text: opt ? clean(opt.text, 40) : '' };
    }
    if (tag === 'textarea') return { value: clean(el.value, 80) };
    if (tag === 'a') return { href: el.getAttribute('href') || '' };
    return {};
  };

  // ---- 6. 收集候选 ----
  // 遍历所有根：light DOM 在前，shadow 里的在后。
  // 顺序有意义——下面的排序是稳定的，同在视口内时按这个顺序排，
  // 所以 shadow 里的搜索入口不会抢走页面主体内容的位置。
  const candidates = [];
  roots.forEach((root) => {
    root.querySelectorAll(SELECTOR).forEach((el) => {
      if (el.disabled) return;          // 禁用的按钮点不动，编号给它纯属浪费
      if (!isVisible(el)) return;       // 隐藏元素不列
      candidates.push(el);
    });
  });

  // ---- 6.5 模态框聚焦 ----
  // 有弹窗打开时，弹窗外的元素**根本点不到**：Playwright 会一直报
  // 「<mdn-search-modal> intercepts pointer events」然后重试到超时。
  // 真实站点（MDN）实测就是这么废掉的：搜索弹窗已经开了，模型还去点被遮住的
  // JavaScript 链接，6 步全打水漂。
  // 所以弹窗开着时只列弹窗里的元素 —— 这既符合事实（外面的确实点不到），
  // 也把模型的注意力收窄到唯一能走通的那条路。
  const MODAL_SELECTOR = 'dialog[open], [role="dialog"], [aria-modal="true"]';
  let modal = null;
  for (const root of roots) {
    for (const found of root.querySelectorAll(MODAL_SELECTOR)) {
      if (isVisible(found)) { modal = found; break; }
    }
    if (modal) break;
  }
  if (modal) {
    const inside = [];
    modal.querySelectorAll(SELECTOR).forEach((el) => {
      if (el.disabled) return;
      if (!isVisible(el)) return;
      inside.push(el);
    });
    candidates.length = 0;
    inside.forEach((el) => candidates.push(el));
  }

  // ---- 7. 排序 + 截断 ----
  // 基础规则：视口内的排前面 —— 用户/Agent 第一眼看到的东西更可能相关。
  // 超出上限的部分直接不列，但把总数报上去，模型可以据此决定要不要滚动。
  //
  // 但表单控件必须保底，不能参与「按位置截断」：
  // 真实站点（MDN 首页）实测 —— 弹窗里的搜索框在 shadow DOM 里，94 个候选只列 80 个时
  // 正好被截掉，模型只能看到一堆 button，报「不能往 button 里输入文字」，任务直接废。
  // 而 input/select/textarea 是「能输入」的唯一入口，数量本来就少（几到十几个），
  // 单独给它们留名额，比按位置排更划算。
  const isField = (el) => ['input', 'select', 'textarea'].indexOf(el.tagName.toLowerCase()) >= 0;
  const keyed = candidates.map((el, i) => ({ el, i, vp: inViewport(el), field: isField(el) }));
  const byPos = (a, b) => (b.vp - a.vp) || (a.i - b.i);
  const fields = keyed.filter((x) => x.field).sort(byPos);
  const others = keyed.filter((x) => !x.field).sort(byPos);
  // 上限的一半（至少 10 个）留给表单控件，避免某个页面几百个 input 把别的位置全挤掉
  const fieldQuota = Math.min(fields.length, Math.max(10, Math.floor(maxElements / 2)));
  const ordered = fields.slice(0, fieldQuota).concat(others.slice(0, maxElements - fieldQuota));

  const elements = ordered.slice(0, maxElements).map((item, idx) => {
    const el = item.el;
    const id = 'e' + (idx + 1);
    el.setAttribute(ATTR, id);  // 打标记：动作层就靠这个属性找到它
    // bbox：包围盒归一化到视口（0~1）。P6 视觉兜底的「元素↔像素」桥——
    // 截图乘尺寸即得像素位置，视觉结论才能对回这里的编号。
    // ⚠️ 只落盘给视觉模块，不进 render_snapshot_text（纪律同 input_type：回放基线）。
    // devicePixelRatio 不用除：getBoundingClientRect 返回 CSS 像素，
    // Playwright 截图默认也按 CSS 像素出（scale=css），两边天然一致。
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || 1;
    const vh = window.innerHeight || 1;
    const bbox = {
      x: +(r.left / vw).toFixed(3),
      y: +(r.top / vh).toFixed(3),
      w: +(r.width / vw).toFixed(3),
      h: +(r.height / vh).toFixed(3)
    };
    return Object.assign(
      { id: id, role: roleOf(el), text: labelOf(el), in_viewport: item.vp, bbox: bbox },
      stateOf(el)
    );
  });

  // ---- 8. 页面级信息 ----
  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .map((h) => clean(h.innerText, 60))
    .filter(Boolean)
    .slice(0, 8);

  // 正文：保留换行。
  // 以前这里是 replace 把所有空白压成空格，换行也一起被压掉 —— 整页变成一行 1200 字符的巨块。
  // 真实站点 R2 就栽在这：page/2 有 10 条引言首尾相连，模型无法判断「哪条在最前面」，
  // 把第 9 条（Mark Twain）当成了第一条。换行是页面结构里最便宜、也最不可替代的语义。
  //
  // 代码里用 NL 常量，不写字面换行：这段 JS 嵌在 Python 三引号字符串里，反斜杠要写两层，
  // 改一次错一次（这里我连踩三回，每次都是 JS 语法错误）。String.fromCharCode(10) 绕开它。
  const NL = String.fromCharCode(10);
  const TAB = String.fromCharCode(9);
  const rawText = (document.body.innerText || '')
    .split(NL)
    .map((line) => line.split(TAB).join(' ').replace(/ +/g, ' ').trim())
    .filter(Boolean)
    .join(NL);

  // 正文截断策略：头 + 尾，不是只留头。
  // 为什么：分页、合计、「共 N 条结果」、提交成功提示这些关键结论天生在页面底部，
  // 只截前半段会把它们全丢掉，模型就会把「没看到」当成「不存在」。
  //
  // 三个「为了省字符反而变蠢」的坑，都是真实站点换来的：
  // 1) 轻微超限就切：正文 1216、上限 1200，为了省 16 个字符把页面劈成两截，
  //    尾段被拼到头部后面，看起来就像「紧跟在开头」—— 模型于是把页尾的第 9 条当成第一条。
  //    所以留 1.25 倍容差：轻微超限就整段保留，切了反而制造假相邻。
  // 2) 死守行边界：第一版切到最近的换行，结果一句引言就 200 字符，头部只剩 19 字符
  //    （刚够两行标题），2979 字符被整段省略 —— 比不按行切还糟。
  //    改成带容差：只有退得不超过 30% 预算时才按行切，否则宁可切断半句。
  // 3) 拼接处必须自解释：标记要明说「下一行是页面末尾」，
  //    否则模型会把尾段当成紧接头部的一句。
  let text = rawText;
  const truncated = rawText.length > maxText * 1.25;
  if (truncated) {
    const headLen = Math.ceil(maxText * 0.6);
    const tailLen = maxText - headLen;
    const headRaw = rawText.slice(0, headLen);
    const tailRaw = rawText.slice(rawText.length - tailLen);

    let head = headRaw;
    const headCut = headRaw.lastIndexOf(NL);
    if (headCut > headRaw.length * 0.7) head = headRaw.slice(0, headCut + 1);

    let tail = tailRaw;
    const tailCut = tailRaw.indexOf(NL);
    if (tailCut >= 0 && tailCut < tailRaw.length * 0.3) tail = tailRaw.slice(tailCut + 1);

    const omitted = Math.max(0, rawText.length - head.length - tail.length);
    text = head + NL +
           '〔中间省略 ' + omitted + ' 字符，你看不到；下一行直接是页面末尾，不是接着上面的〕' + NL +
           tail;
  }
  return {
    url: location.href,
    title: document.title,
    headings: headings,
    text: text,
    text_truncated: truncated,
    elements: elements,
    elements_total: candidates.length,
    modal_open: !!modal,
    elements_shown: elements.length,
    scroll: {
      y: Math.round(window.scrollY),
      viewport: window.innerHeight,
      page_height: document.documentElement.scrollHeight
    }
  };
}
"""


async def take_snapshot(
    page: Page,
    *,
    max_elements: int = MAX_ELEMENTS,
    max_text_chars: int = MAX_TEXT_CHARS,
) -> PageSnapshot:
    """对当前页面拍一张快照。

    调用前要保证页面已经加载完（session.goto 用的是 domcontentloaded）。
    如果页面还在异步渲染，可能出现"快照里没有本该有的元素"——这种情况不是 bug，
    是页面还没渲染完，M4 会靠"连续两次快照一致"来判断页面稳定。
    """
    data: dict[str, Any] = await page.evaluate(
        _SNAPSHOT_JS, {"maxElements": max_elements, "maxText": max_text_chars}
    )
    return data  # type: ignore[return-value]


# "只有截图看得见"的线索探测。视觉兜底条件 1（P6 决策 1）的判断依据：
# 快照里可交互元素很少，不代表页面没信息——可能是 canvas 画的图表、
# 或者一整张大图（地图、海报、验证码）。这些东西在 DOM 里对我们是黑盒，
# 但对截图是透明的，所以"DOM 稀疏但有它们"才是视觉真正能救的场景。
_VISUAL_CUES_JS = """
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = window.getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  };
  const canvas = Array.from(document.querySelectorAll('canvas')).filter(visible).length;
  // "大图"的门槛定 200×200：小图标（logo、箭头）满地都是，
  // 把它们算进来会让条件 1 几乎页页命中，视觉调用量失控
  const big_image = Array.from(document.querySelectorAll('img')).filter(
    (el) => visible(el) && el.naturalWidth >= 200 && el.naturalHeight >= 200
  ).length;
  return { canvas, big_image };
}
"""


async def page_visual_cues(page: Page) -> dict[str, int]:
    """数一下页面上"DOM 给不了信息、只有截图能看见"的东西。

    返回 {"canvas": n, "big_image": n}。探测失败返回全 0——
    这是给视觉兜底做判断用的辅助信息，它自己不能把任务搞挂。
    """
    try:
        data = await page.evaluate(_VISUAL_CUES_JS)
        return {"canvas": int(data.get("canvas", 0)), "big_image": int(data.get("big_image", 0))}
    except Exception:  # noqa: BLE001 —— 探测失败就当"没有"，走 DOM 主路径
        return {"canvas": 0, "big_image": 0}


def selector_for(element_id: str) -> str:
    """把快照里的编号翻译成 Playwright 选择器。

    这是"模型说话"和"浏览器动手"之间的唯一接口：模型只输出 e7，
    我们负责拼成 [data-wp-id="e7"]。

    为什么要校验格式：模型是自由的，它可能返回 "e7'); document.body.remove()" 这种东西。
    校验不过就直接抛错，让上层把它当成一次失败的动作，而不是把脏字符串拼进选择器。
    """
    if not _ID_PATTERN.match(element_id):
        raise ValueError(f"非法元素编号：{element_id!r}（应形如 e1 / e12）")
    return f'[{ID_ATTR}="{element_id}"]'


def render_snapshot_text(snapshot: PageSnapshot) -> str:
    """把快照渲染成给模型看的纯文本。

    为什么不直接把 JSON 丢给模型：
    JSON 的括号引号占掉大量 token，而且字段名（in_viewport / selected_text）
    对模型来说是纯噪音。等宽的两三列文字反而更好读，也更省。
    """
    lines: list[str] = []
    lines.append(f"[URL] {snapshot['url']}")
    lines.append(f"[标题] {snapshot['title']}")

    if snapshot["headings"]:
        lines.append("[标题层级] " + " | ".join(snapshot["headings"]))

    scroll = snapshot["scroll"]
    lines.append(
        f"[滚动] 已滚到 {scroll['y']}px / 页高 {scroll['page_height']}px"
        f"（视口 {scroll['viewport']}px）"
    )

    # 弹窗打开时必须明说，否则模型会以为"页面就这么点东西，是不是没加载完"。
    # 同时告诉它能走的路在哪：弹窗里的输入框就是能输入的地方。
    # 真实站点（MDN）实测：不说这一句时，模型会去点被遮住的链接，重试到超时。
    if snapshot.get("modal_open"):
        lines.append(
            "[提示] 当前有弹窗/对话框打开着，它挡住了页面其它部分（外面的元素现在点不到）。"
            "请先把弹窗里的事做完：弹窗里的输入框是现在唯一能输入的地方。"
        )

    # 截断这件事必须写在标签上，不能只藏在正文里：
    # 模型看到"已截断"才会做出"去滚动"或者"先别下结论"的正确反应。
    # 同时补一句"从上到下"：任务里说"第一条""最上面那条"时，模型需要这个前提才排得出顺序。
    # 这是一句自解释的话，不是要它背下来的图例（图例在真实站点已经翻过一次车，见第 18 条）。
    if snapshot["text_truncated"]:
        body_label = "[正文]（页面从上到下的文字，已截断：中间有省略，省略处已标出）"
    else:
        body_label = "[正文]（页面从上到下的文字）"
    # 正文另起一行：标签和第一行内容挤在一起时，模型容易把标签当成正文的第一句
    lines.append(f"{body_label}\n{snapshot['text']}")

    shown, total = snapshot["elements_shown"], snapshot["elements_total"]
    if total > shown:
        # 把"看不全"变成可执行动作：告诉模型抽屉里还有东西，以及怎么拿到
        head = (
            f"[可交互元素] 共 {total} 个，只列出前 {shown} 个"
            f"（看到的不是全部；标 [屏外] 的元素要先 scroll 才能点）"
        )
    else:
        head = f"[可交互元素] 共 {total} 个"
    lines.append(head)

    for el in snapshot["elements"]:
        # 这里曾经用一个 * 号标记"在视口内"，并在表头写「（* = 在视口内）」。
        # 真实站点实测失败：MDN 首页的「Skip to search」是键盘跳转链接，
        # 被 CSS 藏在视口上方（y=-320），模型想找搜索框，看见 "search" 就点它，
        # Playwright 报「element is outside of the viewport」，重试到超时。
        #
        # 原因不是模型笨，是**图例需要人记**：它要先把 * 的含义存进工作记忆，
        # 再去比对每一行。弱模型不会做这件事。
        # 改成只标记异常项，而且用中文自解释——不需要记任何图例。
        # 副作用：绝大多数元素（视口内）反而少了一个字符，整体略省 token。
        parts = [f"  {el['id']:<4}{el['role']:<9}{el['text']}"]

        if "value" in el and el["value"]:
            parts.append(f"value={el['value']!r}")
        if "checked" in el:
            parts.append(f"checked={el['checked']}")
        if el.get("selected_text"):
            parts.append(f"当前选中={el['selected_text']}")
        if el.get("href"):
            parts.append(f"-> {el['href']}")
        # is False 而不是 falsy：缺字段时不该被当成"屏外"
        if el.get("in_viewport") is False:
            parts.append("[屏外]")

        lines.append("  ".join(parts))

    return "\n".join(lines)
