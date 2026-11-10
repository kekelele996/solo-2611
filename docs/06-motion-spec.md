# 06 · 动效细则

## 1. 动效在本产品中的定位

这个产品每天被打开十几次，每次停留几小时。动效的**唯一合法用途**是：
**说明「什么发生了变化」以及「我点下去之后发生了什么」。**

因此本项目对动效有明确禁令：

| 禁止 | 原因 |
|---|---|
| 入场动画（fade-in / slide-up 逐个出现） | 每天看几十次，第三次就变成拖延 |
| 骨架屏 shimmer 扫光 | 干扰阅读，且掩盖了「加载中」这一信息 |
| 数字滚动计数 | 需要读出准确值，滚动期间读不了 |
| 弹窗缩放弹入（scale 0.8→1） | 突兀，且让「是否要点」的判断延迟 200ms |
| 悬停位移（translateY -2px） | 会让表格行在鼠标移动时抖动，破坏扫描 |
| 状态色渐变过渡（红→黄 800ms） | 状态变化必须瞬时可见 |
| 任何时候 > 300ms 的动画 | 除下述白名单 |

## 2. 时长与缓动表

所有动效必须从下表取值，不允许出现表外数值。

| 类别 | 时长 | 缓动 | 用途 |
|---|---|---|---|
| `instant` | 0ms | — | 状态点、颜色、文字内容变化 |
| `micro` | 80ms | `ease-out` | 按钮按下、checkbox、tab 下划线（仅位移） |
| `quick` | 120ms | `cubic-bezier(.2,0,.3,1)` | 悬停底色、下拉展开、tooltip 出现 |
| `panel` | 180ms | `cubic-bezier(.2,0,.3,1)` | Drawer 滑入、右侧栏折叠、Modal 出现 |
| `layout` | 200ms | `cubic-bezier(.2,0,.3,1)` | SplitPane 拖拽松手后的吸附、Tab 内容高度变化 |
| `pulse` | 1600ms 循环 | `ease-in-out` | 仅「运行中」状态点的呼吸（见 §5.1） |

```css
:root {
  --dur-instant: 0ms;
  --dur-micro: 80ms;
  --dur-quick: 120ms;
  --dur-panel: 180ms;
  --dur-layout: 200ms;
  --ease-out: ease-out;
  --ease-std: cubic-bezier(.2, 0, .3, 1);
}
```

**默认全部 `--dur-instant`**，只有在下列 9 处显式开启动效。

## 3. 九处允许的动效（逐条规格）

### 3.1 运行中状态点呼吸

```
● running
```

```css
@keyframes breathe {
  0%, 100% { box-shadow: 0 0 0 0 rgba(47,107,79,.30); }
  50%      { box-shadow: 0 0 0 4px rgba(47,107,79,0); }
}
.status-dot[data-state="running"] { animation: breathe 1600ms ease-in-out infinite; }
```

- 圆点本身不做缩放或透明度变化，只做外扩光晕（不改变可读性）。
- **同一屏最多 4 个元素同时呼吸**（容器并发上限 4），超过时关闭动画只保留颜色。
- `prefers-reduced-motion` 下完全关闭，颜色仍然区分。

### 3.2 进度条数值推进

运行进度（attempt 内阶段推进）用宽度过渡：

```css
.progress-fill { transition: width 400ms cubic-bezier(.2,0,.3,1); }
```

- 只对**真实进度**过渡，不对「未知进度」做假动画。未知时用 2px 条纹静止条 + `已 34m` 文字。
- 从 0 到首次非零宽度不做动画（直接出现），避免「假装在跑」。

### 3.3 Drawer 滑出

```css
.drawer { transform: translateX(100%); transition: transform 180ms var(--ease-std); }
.drawer[data-open="true"] { transform: translateX(0); }
```

- 遮罩同步 120ms 淡入（不是 180ms），让「背景已不可点」先于面板到位。
- 关闭用 `120ms`，比打开快 —— 关闭时用户已决定离开，没有信息要传达。
- `prefers-reduced-motion`：改为直接 `display` 切换。

### 3.4 Modal 出现

- 只做透明度 + 2px 上移，`120ms`。**不做 scale。**
- 遮罩 `rgba(26,24,21,.32)`，`100ms`。

### 3.5 Tab 切换

- 下划线（2px accent）在 `80ms` 内水平移动到新位置（`transform: translateX`，不改变宽度的 `left/width` 动画）。
- 内容区**不做淡入**，直接切换（用户正在读，淡入等于遮挡）。

### 3.6 折叠展开（轨迹步骤详情）

```css
.step-body { display: grid; grid-template-rows: 0fr; transition: grid-template-rows 200ms var(--ease-std); }
.step-body[data-open="true"] { grid-template-rows: 1fr; }
```

- 只用于**单条轨迹步骤的展开**，一次只展开一条（手风琴），避免高度连锁重排。
- 展开时自动滚动到该步骤顶部（`scrollIntoView({block:'start', behavior: prefersReduced ? 'auto' : 'smooth'})`）。
- 打开后 200ms 内不再响应其他展开请求（防连点）。

### 3.7 Toast 进出

- 进：从下方 8px 位移 + 淡入，`120ms`。
- 出：淡出 + 4px 下移，`100ms`。
- 多条堆叠时其余条目做 `layout` 位移，`120ms`。
- 已显示 3 条时，新来的替换最旧一条（不做 4 条队列），并在新 Toast 上标注「还有 N 条」。

### 3.8 SplitPane 拖拽与吸附

- 拖拽中：`transition: none`，1:1 跟随指针，无任何延迟。
- 松手：若宽度落在预设吸附点（264 / 320 / 400）±16px 内，`200ms` 吸附过去；否则停在原位不做动画。
- 双击分隔条：`200ms` 回到默认宽度。

### 3.9 批量操作的行级反馈

勾选多行后执行批量操作（如批量排队），被操作行：

```
行背景 surface-3 → 保持 600ms → 恢复
```

- 只做背景色切换（`instant` 进入，`600ms` 后 `instant` 恢复），**不做行高变化、不做移除动画**。
- 失败行：左侧 2px `danger` 竖条，保持到用户下次操作。

## 4. 明确不做动画的三类场景

### 4.1 列表增删

新增任务、任务完成移出「进行中」分组：**直接重排，零动画**。
理由：列表每 3 秒轮询刷新，动画会与刷新互相打断，且分组间位置变化本来就需要用户重新定位。

替代方案：新增行左侧出现 2px `info` 竖条，保持 3 秒（静态标记，不闪）。

### 4.2 数字变化

指标条（已导出 12 / 进行中 3 / 卡住 1）数字直接替换。
若某数字增加，该数字后追加一个 6px `ok` 上箭头，2 秒后消失（同上，静态标记）。

### 4.3 状态色变化

`running → ran` 的圆点从绿变灰，**瞬时**。任何过渡都会让用户误以为还在跑。

## 5. 加载与等待的动效策略

| 等待时长 | 表现 | 动效 |
|---|---|---|
| < 200ms | 什么都不显示 | — |
| 200ms–2s | 局部灰条占位（3 行，静态） | 无 |
| 2s–30s（如 repo 推送、快照） | 按钮内 loading：`⟳` 图标 600ms/圈旋转，文案改为「推送中…」 | `spin 600ms linear infinite` |
| > 30s（容器运行） | 不做 spinner，显示**已用时长 + 阶段名**，每 2s 更新文字 | 无 |

**超过 30 秒的等待不给转圈。** 转圈传递不了信息；「已运行 34m12s · 正在运行容器」能。

```css
@keyframes spin { to { transform: rotate(360deg); } }
```

旋转图标只在 2s–30s 区间使用，`prefers-reduced-motion` 时改为静态 `⟳` + 文字「处理中」。

## 6. 键盘交互的动效

| 动作 | 表现 |
|---|---|
| 焦点移动（Tab） | `outline` 立即出现，`instant` |
| 命令面板 `⌘K` 打开 | `panel` 180ms（唯一一个「打开动画」被允许的组件，因为它覆盖全屏，需要交代来源） |
| 命令面板结果列表刷新 | 直接替换，不淡入 |
| 快捷键 `g d`（导航） | 立即跳转，页面无过渡 |

命令面板的 180ms 是从触发位置（顶栏搜索框）向上展开的**高度**过渡，不是缩放。

## 7. 录屏页专用的动效

这是全站唯一允许「大一点」的动效区域，因为它承载的是**时间敏感的物理动作**。

### 7.1 倒计时 3-2-1

```
        3
```

- 数字 72px mono，居中，在 600px 高的遮罩中。
- 每次换数：数字直接替换 + 遮罩圆环（SVG，`stroke-dasharray`）在 1000ms 内从满到空。
- 圆环颜色：3/2 → `--info`，1 → `--warn`。
- 倒数到 0：遮罩 120ms 淡出，并显示 「● 录制中」常驻角标。
- `prefers-reduced-motion`：圆环改为静态显示剩余秒数文字。

### 7.2 录制中计时与剩余秒数

```
● 录制中   00:41 / 01:30   ▓▓▓▓▓▓▓▓▓░░░
```

- 秒数每秒更新（`instant`）。
- 剩余 20 秒时进度条变 `--warn`，剩余 10 秒时变 `--danger` 并每秒闪烁一次（200ms on / 200ms off，仅角标，不闪进度条）。
- **闪烁只在 ≤10 秒开启**，其余时间静止。

### 7.3 录制结束的校验结果出现

- 结果卡直接出现（`instant`），成功/失败用整块 `ok-bg` / `danger-bg` 背景区分。
- 失败时的原因列表逐条静态排列，**不做逐条渐入** —— 用户需要立刻读完全部原因。

## 8. 无障碍与性能约束

### 8.1 `prefers-reduced-motion`

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
    scroll-behavior: auto !important;
  }
}
```

同时需要在 JS 侧尊重：`window.matchMedia('(prefers-reduced-motion: reduce)').matches`
用于 `scrollIntoView` 的 `behavior` 参数。

### 8.2 性能红线

- 所有动画只允许改变 `transform` / `opacity` / `background-color`。
- **禁止动画**：`width`、`height`、`top`、`left`、`margin`、`box-shadow`（除 §3.1 的单点光晕）、`filter`。
- 例外：§3.2 进度条宽度过渡（单个元素、低频率、可接受 repaint）。
- 呼吸动画的 `box-shadow` 最多 4 个并发元素（见 §3.1）。

### 8.3 动画不影响数据

任何动效不得延迟数据可见时间。例如：列表刷新时先渲染数据再考虑动效；按钮点击后状态必须立即更新为「进行中」，不等动画结束。

## 9. 自检清单（开发验收用）

开发完成后逐条检查，任一条不满足即视为未完成：

- [ ] 全站除 9 处白名单外，`transition` 与 `animation` 数量为 0（用 `grep -r "transition" src/` 核对）
- [ ] 无入场动画、无 skeleton shimmer、无数字滚动
- [ ] 状态变化（颜色、文字）在任何情况下都是 `instant`
- [ ] 悬停不产生位置偏移（`translate` 在 `:hover` 中为 0）
- [ ] `prefers-reduced-motion: reduce` 下所有动效关闭且功能完整
- [ ] 30 秒以上的等待不出现 spinner
- [ ] 录屏倒计时与剩余秒数提示按 §7 实现
- [ ] DevTools Performance 录制一次「切换任务详情」，无掉帧（目标 < 16ms/帧）
