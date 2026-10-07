# grokbot2api UI 设计简报（可复用模板）

> 适用：桌面壳层（Tauri + React）与内嵌 `/admin` 工作台（Python 字符串 HTML）。  
> 目标：安静、像成品工具，而不是 AI demo / 霓虹 SaaS。

---

## 1. 以前为什么丑

旧版（约 0.3.9 及更早）常见问题，不是「缺功能」，而是**视觉语言互相打架**：

1. **蓝霓虹 + 深黑**：登录页 `#3b82f6` 按钮、`#0f1419` / `#1a2332` 卡片，和壳层后来的绿强调色完全两套产品。
2. **巨型大写指标卡**：总览像仪表盘海报，字号、留白、描边都偏「展示页」，不像日常操作台。
3. **iframe 主题错位**：壳层暗色、admin 另一套色；或壳层有侧栏、admin 再嵌一套侧栏（双导航）。
4. **占位当图标**：侧栏 `nav-dot`（5px 圆点）、emoji、或空方块 brand-mark——一眼原型感。
5. **表单汤**：字段密、标签弱、主按钮到处都是，分不清「今天要点的」和「偶尔用的」。
6. **状态横幅当主 UI**：成功/失败 sticky banner 占版面；应是短暂 toast。
7. **AI demo 密度**：字号偏大、卡片厚度高、装饰多于信息；桌面工具应偏 Linear / Raycast 密度。
8. **登录像另一个 App**：浏览器打开 `/admin/login` 仍是蓝霓虹，和壳内工作台割裂。
9. **空态墙**：表格里一整行「暂无数据」大字，没有层级、没有图标、没有下一步暗示。
10. **图标系统缺失**：有的地方 emoji、有的地方点、有的地方纯文字——没有统一 stroke 语言。

---

## 2. 这次为什么好看

0.3.10–0.3.13 真正起作用的，不是「换了个好看的字体」，而是：

| 改动 | 作用 |
| --- | --- |
| **同一套 token**（壳 ↔ admin） | `--bg #0c0c0d`、绿 accent、同一 border/text 阶梯；亮/暗对称 |
| **密度** | UI 约 12–13px；行高紧；指标条而非大海报卡 |
| **按钮层级** | primary 稀缺；secondary / ghost 承担日常；危险操作用文字色而非大红块 |
| **安静表格** | sticky 表头、细分割线、hover 一行；空态一行字 + muted 图标 |
| **主题桥** | `?theme=` + `postMessage({type:'grokbot2api-theme'})`；embed 藏侧栏/退出 |
| **stroke 图标** | 16×16、1.5–1.75 描边、`currentColor`；active 用 accent **或** 浅底，不同时尖叫 |
| **Toast 替代 sticky status** | 成功约 3s / 错误约 6s，不抢主内容 |
| **登录同源** | `/admin/login` 跟工作台同一 token；`prefers-color-scheme`；正经 focus |

一句话：**一个视觉语言，两处渲染（React 壳 + Python HTML），共享原则而不是各画各的。**

---

## 3. 设计原则速查

- [ ] 参考安静桌面工具（Linear / Raycast），不是 Marketing Landing。
- [ ] 先做 token，再堆组件；亮暗成对。
- [ ] 12–13px UI 字；标题最多 14–15px；少用巨大数字墙。
- [ ] Primary 按钮「页面里少见」；默认 secondary/ghost。
- [ ] 图标：单色 stroke，14–16px，点按区 20–24px；不要 emoji / Font Awesome CDN。
- [ ] Active 导航：浅底 **或** 图标变 accent，不要又粗描边又荧光。
- [ ] 表格：sticky header、紧 padding、空态一行。
- [ ] 反馈：toast；不要常驻成功绿条。
- [ ] Embed / iframe：壳管导航，内页藏重复 chrome。
- [ ] 登录 / docs / admin **同一产品脸**；禁止第二套蓝霓虹。
- [ ] 禁止：渐变光斑、Inter+紫 SaaS 套路、巨型 uppercase 卡片、装饰性玻璃拟态。

---

## 4. 下次让 AI 写 UI 的提示词模板

把下面整段复制给任何模型（可按文件路径改）：

```text
你在改 grokbot2api 的桌面 UI（Tauri 2 + React 壳）和/或 Python 内嵌 admin HTML。

视觉参考：Linear / Raycast 那种安静桌面工具——密度高、装饰少、强调色克制。
已有壳层 token（必须沿用，禁止另起炉灶）：
- 暗色 bg #0c0c0d / elevated #121214 / border #232326 / text #ececef / muted #6b6b74
- accent 绿 #3d9a6a（勿改回蓝霓虹 #3b82f6）
- 亮色对称一套；UI 字号约 12–13px；radius 6–8px

硬性禁止：
- 蓝霓虹、紫色 Inter SaaS、渐变光斑、玻璃拟态、巨型 uppercase 指标卡
- emoji 当图标、Font Awesome CDN、lucide 新依赖（除非我明确同意）
- 第二套视觉语言（登录页 / docs / admin 必须跟壳层同一脸）
- sticky 成功横幅；用短暂 toast
- 双导航（embed 时藏内页侧栏）

图标规则：
- 内联 SVG，16×16，stroke 1.5–1.75，currentColor，圆角线帽
- 导航 active：图标变 accent 或浅底，二选一为主
- 按钮可 text+icon；纯 icon 必须有 title/aria-label

交互/结构：
- 保留现有 DOM id 与 /admin/api/* 行为
- 主题：?theme= + postMessage；亮暗跟随壳
- 主按钮稀缺；表格 sticky header；空态一行短文案 + muted 图标

验收（全部满足再交）：
1) 截图/预览里看不到蓝霓虹与 emoji 图标
2) 壳与 admin 色板一致（含登录页）
3) 侧栏是真图标不是圆点
4) embed 无双侧栏
5) 空态不是「暂无数据」墙
6) 字号与密度接近现有 App.css，而不是 landing page
```

---

## 5. 验收清单

1. [ ] 暗色下 accent 为绿，不是蓝  
2. [ ] 亮色可切换且 admin iframe 同步  
3. [ ] 侧栏每个 NavId 有 stroke 图标  
4. [ ] brand-mark 不是空色块（有简单字母/桥标）  
5. [ ] `/admin/login` 与工作台同 token  
6. [ ] embed=1 时无重复侧栏/退出  
7. [ ] 表格空态为一行 + muted 图标  
8. [ ] 主按钮在单页中明显少于次要按钮  
9. [ ] 无 emoji 图标、无 FA CDN  
10. [ ] 成功/失败用 toast，非常驻横幅  
11. [ ] 字号 12–13px 主导，无巨型卡片墙  
12. [ ] 焦点态可见（输入框 / 主按钮）

---

## 维护提示

- 壳层图标：`desktop/src/icons.tsx`  
- 壳层样式：`desktop/src/App.css`  
- Admin HTML：`grokbot2api.py` 内 `ADMIN_LOGIN_HTML` / `render_admin_html`  
- 预览：`desktop/admin-preview.html`（改完 admin 后请同步）  
- 改网关后：`cd desktop && npm run sync-gateway`
