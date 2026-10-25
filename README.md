# 多站点多级页面抓取 → GitHub Gist

一个由 GitHub Actions 定时驱动的网页抓取器：

- ✅ **多级页面抓取**：列表页 → 详情页 → 更深层级，任意深度
- ✅ **未知层级递归抓取**：不知道页面有多少级时，沿链接自动深入，遇到目标节点即提取（`crawl` 模式）
- ✅ **链式抽取**：字段支持 CSS 选择器 → XPath → 正则 逐步串联，实现复杂抽取逻辑
- ✅ **CSS 选择器**提取字段（取文本 / 取属性 / 多值合并 / 正则二次提取 / 截断）
- ✅ **静态 + 动态页面，自动判断**：默认 `render: auto`——先静态抓取并用选择器校验内容，拿不到自动切换 Playwright 渲染（每个页面单独判断）；也可强制 `static` / `dynamic`
- ✅ **多站点配置**：一个 `config.yaml` 管理所有站点，单站点可独立启停
- ✅ **结果输出到 Gist**：每个站点一个文件，txt（每条一行）/ Markdown / JSON 格式
- ✅ **瞬时失败自动重试**：超时 / 5xx / 429 自动重试（`retry` 可配），动态渲染加载失败同样重试
- ✅ **敏感信息不落仓库**：配置值支持 `${环境变量}` 占位符，Cookie 等通过环境变量/Secret 注入
- ✅ 所有行为均由 `config.yaml` 配置

## 文件结构

```
├── config.yaml                 # 实际运行的配置（示例站点已停用，正式站点在此维护）
├── config-example.yaml         # 完整注释的示例配置（演示站点全部启用，供参考/复制）
├── crawler.py                  # 抓取脚本
├── requirements.txt            # Python 依赖
├── requirements-dev.txt        # 开发依赖（pytest）
├── tests/                      # 单元测试（不访问网络）
└── .github/workflows/crawler.yml   # GitHub Actions 定时任务
```

## 快速开始

### 1. 创建 Gist Token

GitHub → Settings → Developer settings → **Personal access tokens (classic)** → Generate new token，勾选 **`gist`** 权限。
（注意：Gist API 不支持 fine-grained token，请使用 classic token。）

### 2. 配置仓库 Secret

把本项目文件推到一个 GitHub 仓库（public / private 均可），然后：

仓库 Settings → Secrets and variables → Actions → New repository secret：

| Secret | 必填 | 说明 |
|---|---|---|
| `GIST_TOKEN` | ✅ | 上一步创建的 token |
| `GIST_ID` | ❌ | 指定已存在的 Gist；不填则首次运行自动创建 |

### 3. 运行

- 自动：默认**每 6 小时**运行一次（cron 在 `crawler.yml` 里，按需修改）
- 手动：Actions 页面 → Multi-site Crawler → Run workflow

首次运行会自动创建一个 secret Gist，其 URL 打印在运行日志里，ID 保存到 `gist_id.txt` 并自动提交回仓库；之后每次运行原地更新同一个 Gist。

### 4. 查看抓取结果（访问 Gist）

抓取结果全部写在一个 secret Gist 里，每个站点一个文件（文件名 = 站点 `name` + 扩展名，见「站点 name 的作用与注意点」）。Gist 地址在首次运行后**永远不变**，之后每次定时运行原地更新同一批文件。

**三种访问方式**

| 方式 | 地址 | 适用场景 |
|---|---|---|
| 网页 | `https://gist.github.com/<用户名>/<gist_id>` | 人看，可切换历史版本 |
| raw 直链 | `https://gist.githubusercontent.com/<用户名>/<gist_id>/raw/<文件名>` | 程序 / 客户端订阅（如订阅转换、代理客户端里填订阅 URL） |
| API | `GET https://api.github.com/gists/<gist_id>` | 自动化处理（返回 JSON，`files` 字段含全部内容） |

- raw 直链**去掉中间的版本号段**（`/raw/` 后直接跟文件名）即永远指向最新内容；带版本号的 raw 地址（`/raw/<commit-sha>/<文件名>`）固定指向某一次的历史版本。
- secret Gist 知道链接就能访问（无需登录 GitHub），但不会被搜索引擎收录；想公开可在 Gist 页面 Edit → Make public。

**地址在哪里找**

1. 首次运行：Actions 运行日志里搜 `已创建新 Gist:`，后面就是 URL；脚本同时把 ID 写进 `gist_id.txt` 并自动提交回仓库。
2. 之后想找回：任一次运行日志里搜 `已更新 Gist:`；或打开仓库根目录的 `gist_id.txt`，按 `https://gist.github.com/<你的用户名>/<ID>` 拼出地址；GitHub 主页头像 → **Your gists** 列表里也能看到。
3. 最直接：登录 GitHub 账号后直接访问 <https://gist.github.com>，即可看到该账户下的所有 Gist，点击对应条目即可查看抓取结果；点进后地址栏 `https://gist.github.com/<用户名>/<gist_id>` 末段就是 **gist_id**，需要配置 `GIST_ID` Secret 或拼 raw 直链时可以直接从这里复制（`gist_id.txt` 里存的也是它）。

### 5. 本地调试（可选）

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt     # 跑单元测试用（可选）
python -m playwright install chromium   # 仅配置了动态页面时需要
python -m pytest tests/ -q              # 单元测试，不访问网络（可选）
python crawler.py --dry-run             # 结果写入本地 output/ 目录，不推送 Gist
```

## config.yaml 配置说明

### 全局设置

| 字段 | 说明 |
|---|---|
| `delay` | 相邻两次请求的间隔秒数（默认 `1.0`） |
| `timeout` | 单个请求超时秒数（默认 `30`） |
| `retry` | 瞬时失败（超时 / 连接重置 / 5xx / 429）的重试次数（默认 `2`）：静态请求带指数退避，动态渲染加载失败同样重试 |
| `timezone_offset` | 输出时间戳使用的时区偏移小时数（默认 `8`） |

### 站点级

| 字段 | 说明 |
|---|---|
| `name` | 站点标识，**必填**；决定 Gist 输出文件名，详见下方「站点 name 的作用与注意点」 |
| `title` | 输出 Markdown 的标题（可选） |
| `enabled` | `false` 时跳过该站点 |
| `start_url` / `start_urls` | 入口页（单个别名或列表） |
| `render` | `auto`（默认，先静态、检测不到内容自动切 Playwright）/ `static` / `dynamic` |
| `headers` | 自定义请求头，如 Cookie（可选） |
| `levels` | 预设层级的多级抓取配置，见下（与 `crawl` 二选一） |
| `crawl` | 递归抓取配置（未知层级），见「递归抓取模式」一节 |
| `output.format` | `txt`（默认，每条记录一行）/ `markdown` / `json` |
| `output.separator` | txt 格式行内字段值的分隔符，默认 `" | "`，可设 `"\t"`（方便导入表格） |
| `output.with_keys` | txt 格式行内是否带字段名，如 `author: Albert Einstein`（默认 `false`） |
| `output.inherit_fields` | txt 格式是否把上层条目的字段拼进行内（默认 `true`，即列表页字段 + 详情页字段合成一行） |

### 站点 name 的作用与注意点

`name` 是每个站点的必填标识，决定该站点在 Gist 里的输出**文件名**：清洗后的 `name` + 扩展名（`.txt` / `.json` / `.md`，由 `output.format` 决定），同时也出现在运行日志和配置校验的错误信息里。注意：

- **必填**：缺少 `name` 时启动校验直接报错，不会开始抓取。
- **不能重复**：两个站点清洗后的 `name` 相同，输出文件名就相同，在同一个 Gist 里会互相覆盖（只留一个）。
- **非法字符自动替换**：`\ / : * ? " < > |` 和空白会替换成 `-`，首尾 `-` 去掉；清洗后为空则兜底为 `site`。中文名保留，如 `name: 我的 站点/A` → 文件名 `我的-站点-A.txt`。
- **改名 = 换文件**：`name` 一改，下次推送生成新文件，旧文件作为「不再对应任何启用站点的残留」被自动清理；如有外部链接/订阅引用旧文件名，需自行更新。

### 敏感信息（Cookie 等）

`config.yaml` 会提交到仓库，Cookie、Token 之类的值不要直接写在里面。配置字符串支持 `${VAR}` 占位符，加载时自动替换为环境变量 `VAR` 的值（未设置的保持原样）：

```yaml
    headers:
      Cookie: "${QUOTES_COOKIE}"
```

本地运行先在 shell 里 `export QUOTES_COOKIE=...`；GitHub Actions 里把它配置为仓库 Secret，并在 `crawler.yml` 的 Run crawler 步骤的 `env:` 下加一行 `QUOTES_COOKIE: ${{ secrets.QUOTES_COOKIE }}`。

### 层级级（levels 数组的每一项）

| 字段 | 说明 |
|---|---|
| `name` | 层级名称 |
| `item_selector` | 本层每一项的选择器；**不写 = 整页作为一项** |
| `fields` | 要提取的字段，见下 |
| `url_field` | **写在下一层**：上一层的哪个字段作为本层入口链接 |
| `limit` | 本层最多处理多少项（防止抓取失控） |
| `render` | 覆盖站点级渲染方式（如列表页静态、详情页动态） |
| `wait_for` / `settle` / `wait_timeout` | 动态模式：等待元素出现 / 额外等待秒数 / 等待超时 |

### 字段级（fields 的每个值）

```yaml
fields:
  title: "h3 a"                  # 简写：选择器，取文本
  link:
    selector: "a"
    attr: "href"                 # 取属性；href/src 自动补全为绝对链接
  tags:
    selector: ".tag"
    all: true                    # 匹配所有元素并合并，默认只取第一个
  content:
    selector: ".article"
    html: true                   # 保留内部 HTML（主要给 json 输出用）
    max_length: 500              # 截断过长文本
  date:
    selector: ".time"
    attr: "datetime"
    regex: "\\d{4}-\\d{2}-\\d{2}"   # 对提取值再做正则提取
    default: "未知"              # 未匹配时的默认值
```

### 多级抓取的工作方式（levels 模式）

1. 从 `start_url` 开始，按 `levels` 顺序逐层处理；
2. 每层用 `item_selector` 切分出条目，用 `fields` 从每条提取数据；
3. 下一层声明 `url_field`，指向上层某个字段（通常是链接），脚本就会抓取该链接并把结果挂为子节点；
4. 所有层级处理完后得到树形结构，渲染成 Markdown（嵌套列表）或 JSON；
5. 同一站点内 URL 自动去重，避免重复抓取和死循环。

## 递归抓取模式（未知层级）

不知道页面有多少级、只想抓到符合要求的节点时，用 `crawl` 代替 `levels`：沿链接自动深入，**每访问一页都提取目标节点**，直到没有新链接或达到上限。

```yaml
  - name: my-site
    start_url: https://example.com/docs/
    render: auto
    crawl:
      links: "css:.sidebar a"       # 每页沿这些链接继续深入 (链式选择器, 取 href)
      allow: ["^https://example\\.com/docs/"]   # 可选: 只跟随匹配的链接
      deny: ["/logout"]             # 可选: 排除匹配的链接
      same_host: true               # 默认只跟随同域名链接
      max_depth: 5                  # 最多深入几层链接 (默认 3)
      max_pages: 50                 # 最多访问多少页 (默认 50, 防爆)
      max_items: 200                # 可选: 最多提取多少条记录
      include_page_url: true        # 可选: 每条记录附加 _page 字段 (来源页)
      targets:
        item: "css:div.doc"         # 目标节点: 每个匹配项一条记录 (不写=整页一条)
        fields:                     # 字段用链式抽取, 见下一节
          title: ["css:h1", "regex:^(.+?)\\s*\\|"]
          content: "css:.content"
    output:
      format: txt
```

行为要点：BFS 逐层深入；URL 自动去重（忽略 `#锚点`）；每页的提取与 `render: auto` 判断复用同一套逻辑；结果为扁平记录列表，txt 输出时每条一行。

## 链式抽取（CSS → XPath → 正则）

`levels` 模式和 `crawl` 模式的字段都支持链式写法——多个步骤依次执行，**前一步的结果是后一步的上下文**：

```yaml
fields:
  title: "h1"                            # 普通字符串 = CSS 单步 (兼容原写法)
  title: "xpath://h1/a"                  # 前缀指定单步引擎
  title: ["css:.content", "xpath:./p[1]"]           # 多步链
  link: {chain: ["css:a", "xpath:./@href"], url: true}   # 链式 + 选项
```

步骤规则：

| 步骤 | 输入 → 输出 | 说明 |
|---|---|---|
| `css:选择器`（或不带前缀） | 节点集合 → 节点集合 | CSS 选择器，选中所有匹配 |
| `xpath:表达式` | 节点集合 → 节点/字符串 | 原生 XPath；`.//` 限定在当前节点内，`//` 从整文档找；可直接返回 `text()`、`@attr` |
| `regex:正则` | 文本 → 字符串 | 对每个节点文本（或上一步字符串）匹配提取；**有分组取第 1 组**，无分组取整体匹配 |

约束与技巧：`regex` 之后的步骤不能再是 `css`/`xpath`；链尾默认取节点文本；配合字段选项 `all`（合并所有匹配）、`url: true`（相对链接转绝对）、`max_length`、`default` 使用。（XPath 语法不熟悉的话，直接跳到文末[「附录：XPath 知识速成」](#附录xpath-知识速成)）

实测示例（三步链，从作者链接截取 ID）：

```yaml
author_id:
  chain:
    - "css:a[href*='/author/']"   # 1. CSS 选中作者链接 <a>
    - "xpath:./@href"             # 2. XPath 取 href 属性值
    - "regex:/author/(.+)$"       # 3. 正则截取 -> "Albert-Einstein"
```

## 输出示例

**txt 格式**（默认，每个站点一个 `.txt` 文件，每条记录一行）：

```
"The world as we have created it ..." | Albert Einstein | https://quotes.toscrape.com/author/Albert-Einstein | Albert Einstein | March 14, 1879 | In 1879, Albert Einstein was born in Ulm, Germany. …
"It is our choices, Harry, that show what we truly are ..." | J.K. Rowling | https://quotes.toscrape.com/author/J-K-Rowling | J.K. Rowling | July 31, 1965 | See also: Robert Galbraith …
```

**Markdown 格式**（嵌套列表，保留层级结构）：

```markdown
# 经典语录·静态页示例

> 来源: https://quotes.toscrape.com/
> 抓取时间: 2026-10-25 02:35:34

- **quote**: "The world as we have created it ..."
- **author**: Albert Einstein
- **link**: https://quotes.toscrape.com/author/Albert-Einstein
  - **name**: Albert Einstein
  - **born**: March 14, 1879
  - **about**: In 1879, Albert Einstein was born in Ulm, Germany. …
```

txt 格式的拍平规则：每个**最深层**的条目输出一行；该条目自身及其上层条目的字段值按顺序拼接（`inherit_fields: false` 可关闭继承）；文本内的换行会压成空格，保证一条确实占一行。

## 常见问题

- **Gist 是公开的吗？** 自动创建的是 secret Gist（搜索不到，但知道链接的人可以访问）。想完全公开可在 Gist 页面点 Edit → Make public。
- **忘了 Gist 地址？** 见「快速开始 → 查看抓取结果」：运行日志搜 `已更新 Gist:`，或看仓库根目录的 `gist_id.txt`。
- **定时任务不跑了？** GitHub 会在仓库 60 天无活动后自动暂停 schedule workflow，push 任意 commit 或手动跑一次即可恢复。
- **站点改名/删除后 Gist 里的旧文件还在？** 推送时会自动清理：Gist 中属于爬虫输出（`.md`/`.txt`/`.json`）但不对应任何启用站点的文件会被删除；本次抓取失败的站点保留旧内容不动。
- **Gist 被手动删除了？** 下次运行会自动重建新 Gist 并更新 `gist_id.txt`；若在 Secret 里指定了 `GIST_ID`，记得同步更新。
- **抓不到内容 / 被反爬？** 增大全局 `delay`；需要登录的站点配置 `headers` 里的 Cookie；JS 渲染慢的页面调大 `settle`。
- **自动判断的原理？** `render: auto` 先直接请求 HTML，检查配置的 `item_selector`（或字段选择器）能否匹配到元素：能则用静态结果（快）；不能（或请求被拒）则自动改用 Playwright 渲染后再提取。同一站点内每个页面单独判断，且某页一旦需要动态渲染，同站后续页面会直接走动态以省时间。如果某站骨架 HTML 恰好包含同名选择器导致误判，显式指定 `render: dynamic` 即可。
- **中文乱码？** 脚本已对未声明编码的页面自动探测（`apparent_encoding`），一般无需处理。
- **只想本地看看结果？** `python crawler.py --dry-run`，结果在 `output/` 目录。

## 附录：XPath 知识速成

XPath（XML Path Language）用"路径表达式"在 HTML/XML 的节点树里定位内容。本项目的链式步骤 `xpath:表达式` 用的就是它。会 CSS 选择器能应付大部分场景，但 XPath 能做到 CSS 做不到的事：**按文本找节点、找父级/兄弟节点、直接取出属性和文本**。

### 1. 一切从节点树开始

浏览器把 HTML 解析成一棵树，XPath 就是在树上"寻路"：

```html
<html>                        ← 祖先 (ancestor)
  <body>
    <div class="list">        ← li 的父节点 (parent)
      <li>第一条 <b>加粗</b></li>   ← div 的子节点 (child)、html 的后代 (descendant)
      <li>第二条</li>          ← 上一个 li 的兄弟节点 (sibling)
    </div>
  </body>
</html>
```

### 2. 基本寻路语法

| 表达式 | 含义 |
|---|---|
| `div` | 当前节点的 div **子**元素 |
| `.//div` | 当前节点**内部任意层级**的所有 div（链式抽取最常用） |
| `//div` | 从**整篇文档**找所有 div（注意：会跳出当前上下文） |
| `.//div//p` | div 内任意层级的 p |
| `.//div/p` | div 的**直接**子级 p |
| `./@href` | 当前节点的 href 属性值（直接返回字符串） |
| `.//a/@href` | 所有 a 的 href（返回字符串列表） |
| `.//p/text()` | p 的直接文本 |
| `.//a \| .//p` | 并集：同时选 a 和 p |

> **本项目要点**：链式步骤 `xpath:` 的上下文是上一步选中的节点——想"在上一步结果里继续找"必须写 `.//`；直接返回字符串的写法（`/@href`、`/text()`）特别适合作为链的收尾。

### 3. 谓词 `[ ]`：按条件筛选

紧跟在节点名后面。**位置下标从 1 开始，不是 0！**

| 表达式 | 含义 |
|---|---|
| `.//li[1]` | 每个父级下的第 1 个 li |
| `.//ul/li[3]` | ul 下第 3 个 li |
| `.//li[last()]` | 最后一个 |
| `.//li[last()-1]` | 倒数第二个 |
| `.//li[position()<=3]` | 前 3 个 |
| `.//div[@class='content']` | class **完全等于** content 的 div |
| `.//a[@href]` | 有 href 属性的 a |
| `.//div[contains(@class,'item')]` | class 包含 item（多 class 元素常用） |
| `.//a[starts-with(@href,'https')]` | href 以 https 开头 |
| `.//p[contains(text(),'限时')]` | 文本包含"限时"（CSS 做不到！） |
| `.//input[@type='checkbox' and @checked]` | 多条件 and / or |

class 匹配陷阱：`@class='item'` 要求整串完全相等，`class="item active"` 匹配不上，要用 `contains(@class,'item')`。CSS 的 `.item` 天然按"词"匹配，这点 CSS 更省心。

### 4. 常用函数

| 函数 | 作用 | 示例 |
|---|---|---|
| `text()` | 取直接文本 | `.//h2/text()` |
| `contains(a, b)` | a 包含 b | `contains(text(), '热销')` |
| `starts-with(a, b)` | a 以 b 开头 | `starts-with(@id, 'post-')` |
| `normalize-space()` | 压缩空白后取值/比较 | `normalize-space(text())='详情'` |
| `not()` | 取反 | `not(contains(@class,'ad'))` |
| `string-length()` | 长度 | `string-length(text())>10` |

### 5. 轴（Axes）：往旁边、往上面找

CSS 只能"往下"找后代，XPath 还能定位**父级、祖先、兄弟**——目标信息和入口节点不在同一分支时特别有用：

| 轴 | 含义 | 示例 |
|---|---|---|
| `parent::*` | 父节点 | `./parent::div/@id` |
| `ancestor::div[1]` | 最近的 div 祖先 | |
| `following-sibling::*` | 之后的所有兄弟 | `./following-sibling::span[1]`（紧邻其后的 span） |
| `preceding-sibling::*` | 之前的所有兄弟 | `./preceding-sibling::h3[1]/text()` |

典型场景——价格在标题同一行的右侧，先定位标题再找它的兄弟：

```yaml
price:
  chain:
    - "css:span.title"
    - "xpath:./following-sibling::span[@class='price']/text()"
```

### 6. CSS ↔ XPath 对照表

会 CSS 的话照着迁移：

| 想要什么 | CSS | XPath |
|---|---|---|
| 所有 h1 | `h1` | `.//h1` |
| 按 class | `.item` | `.//*[contains(@class,'item')]` |
| 按 id | `#main` | `//*[@id='main']` |
| 有某属性 | `a[rel]` | `.//a[@rel]` |
| 属性包含 | `a[href*='detail']` | `.//a[contains(@href,'detail')]` |
| 直接子级 | `ul > li` | `.//ul/li` |
| 任意后代 | `div p` | `.//div//p` |
| 第 n 个 | `li:nth-child(3)` | `.//li[3]` |
| 按文本找 | ❌ 做不到 | `.//a[contains(text(),'下一页')]` |
| 找父级/兄弟 | ❌ 做不到 | `./parent::*`、`following-sibling::*` |
| 直接取属性值 | ❌ 做不到 | `.//a/@href` |

### 7. 常见坑与调试技巧

1. **下标从 1 开始**：`li[0]` 永远选不到任何东西。
2. **`//` 与 `.//` 的区别**：本项目的链式抽取里，`xpath://p` 搜的是整篇文档而不是上一步的节点；想在上一步结果内部继续找，必须写 `.//p`。
3. **引号嵌套**：XPath 属性用单引号，YAML 里整个表达式用双引号包裹（反之亦可），避免转义地狱。
4. **`text()` 只取直接文本**：`<p>A<b>B</b></p>` 里 `p/text()` 只得到 A；要全部文本取 `p//text()`，或省略 `text()` 让本项目取节点全文。
5. **现场调试**：浏览器 F12 → Console 输入 `$x('//h1')` 回车，立即看到匹配结果；Chrome 的 Elements 面板 Ctrl+F 也能输入 XPath 搜索节点。
