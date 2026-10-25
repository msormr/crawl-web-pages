#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多站点多级页面抓取器
- 静态页面: requests + BeautifulSoup
- 动态页面(JS 渲染): Playwright (Chromium, 按需懒加载)
- 两种抓取模式:
    levels: 预设层级的多级抓取 (每层配选择器)
    crawl:  递归抓取 (未知层级, 沿链接深入, 出现目标节点即提取)
- 字段抽取支持 CSS 选择器 / XPath / 正则 链式调用
- 结果输出到 GitHub Gist

用法:
    python crawler.py [config.yaml路径] [--dry-run]
    --dry-run: 不推送 Gist, 结果写入本地 output/ 目录
"""

import argparse
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
import soupsieve
import yaml
from bs4 import BeautifulSoup
from cssselect import GenericTranslator
from lxml import html as lxml_html
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GIST_ID_FILE = os.path.join(BASE_DIR, "gist_id.txt")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 MultiSiteCrawler/1.0"
)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Fetcher:
    """页面获取器：静态页用 requests，动态页用 Playwright（首次用到才加载）"""

    def __init__(self, timeout=30, delay=1.0, retry=2):
        self.timeout = timeout
        self.delay = delay
        self.retry = max(0, int(retry))
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        # 瞬时失败(超时/连接重置/5xx/429)自动重试, 指数退避
        retryer = Retry(total=self.retry, connect=self.retry, read=self.retry,
                        backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=frozenset(("GET", "HEAD")),
                        raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retryer)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._pw = None
        self._browser = None

    def _throttle(self):
        if self.delay > 0:
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()

    def fetch(self, url, render="static", wait_for=None, wait_timeout=15,
              settle=0.0, headers=None, best_effort=False):
        self._throttle()
        if render == "dynamic":
            return self._fetch_dynamic(url, wait_for, wait_timeout, settle,
                                       headers, best_effort)
        return self._fetch_static(url, headers)

    def _fetch_static(self, url, headers):
        resp = self.session.get(url, timeout=self.timeout, headers=headers)
        resp.raise_for_status()
        # 目标页未声明编码时自动探测，避免中文乱码
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding
        return resp.text

    def _fetch_dynamic(self, url, wait_for, wait_timeout, settle, headers,
                       best_effort=False):
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        page = self._browser.new_page(user_agent=USER_AGENT,
                                      extra_http_headers=headers or {})
        try:
            last_exc = None
            for attempt in range(self.retry + 1):
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=self.timeout * 1000)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < self.retry:
                        wait_s = 1.5 * (attempt + 1)
                        log(f"  [重试] 动态加载失败({exc}), {wait_s:g}s 后重试: {url}")
                        time.sleep(wait_s)
            if last_exc is not None:
                raise last_exc
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=wait_timeout * 1000)
                except Exception:
                    # best_effort: auto 模式回退时, 等不到元素也用当前内容尽力提取
                    if not best_effort:
                        raise
                    log(f"  [自动] 等待元素 {wait_for!r} 超时, 使用已渲染内容继续")
            if settle > 0:
                page.wait_for_timeout(int(settle * 1000))
            return page.content()
        finally:
            page.close()

    def close(self):
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(obj):
    """递归展开配置字符串里的 ${VAR} 环境变量 (未设置的保持原样)。
    Cookie 等敏感信息因此可以放在仓库 Secret 而不是 config.yaml 里。"""
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    if isinstance(obj, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    return obj


# ============================================================
# 链式抽取引擎: CSS 选择器 / XPath / 正则 逐步串联
#   每一步形如 "css:..." / "xpath:..." / "regex:..."，
#   不带前缀的字符串视为 CSS。前一步的结果作为后一步的上下文:
#     css/xpath 步骤: 节点集合 -> 节点集合 (xpath 也可直接返回字符串, 如 ./@href)
#     regex 步骤:     取每个节点文本(或上一步字符串)做匹配, 有分组取第 1 组
# ============================================================

def compile_step(raw):
    raw = str(raw).strip()
    if raw.startswith("xpath:"):
        return ("xpath", raw[6:].strip())
    if raw.startswith("regex:"):
        return ("regex", re.compile(raw[6:].strip(), re.S))
    sel = raw[4:].strip() if raw.startswith("css:") else raw
    if not sel:
        raise ValueError(f"链式步骤为空: {raw!r}")
    try:
        return ("xpath", GenericTranslator().css_to_xpath(sel))
    except Exception as exc:
        raise ValueError(f"无效的 CSS 选择器 {sel!r}: {exc}")


def compile_chain(steps):
    return [compile_step(s) for s in as_list(steps)]


def run_steps(scope, steps):
    """在 lxml 节点上执行链式步骤, 返回元素/字符串混合列表。"""
    items = [scope]
    str_mode = False  # regex 步骤把结果变成字符串, 之后不能再接 css/xpath
    for kind, expr in steps:
        if kind == "xpath":
            if str_mode:
                raise ValueError("regex 步骤之后不能再接 css/xpath 步骤")
            items = [r for node in items for r in node.xpath(expr)]
        else:  # regex
            str_mode = True
            if items and isinstance(items[0], str):
                texts = items
            else:
                texts = [" ".join(el.itertext()) for el in items]
            found = []
            for t in texts:
                m = expr.search(t or "")
                if m:
                    try:
                        found.append(m.group(1) if expr.groups else m.group(0))
                    except (IndexError, re.error):
                        found.append(m.group(0))
            items = found
    return items


def finalize_values(items, base_url=None, attr=None, absolute=False):
    """把链式结果转成字符串列表; 元素默认取文本, 可指定属性。"""
    values = []
    for it in items:
        if isinstance(it, str):
            v = it
        elif hasattr(it, "itertext"):
            v = it.get(attr) if attr else " ".join(it.itertext())
        else:
            v = str(it)
        v = re.sub(r"\s+", " ", str(v or "")).strip()
        if not v:
            continue
        if absolute:
            v = urljoin(base_url, v)
        values.append(v)
    return values


def chain_field_values(scope, steps, opts, base_url):
    """执行链式抽取并应用字段选项 (all/url/max_length/default)。"""
    compiled = compile_chain(steps)
    values = finalize_values(run_steps(scope, compiled),
                             base_url=base_url,
                             absolute=bool(opts.get("url")))
    if opts.get("all"):
        value = ", ".join(values)
    else:
        value = values[0] if values else ""
    if not value:
        value = opts.get("default", "")
    max_len = opts.get("max_length")
    if isinstance(max_len, int) and 0 < max_len < len(value):
        value = value[:max_len].rstrip() + "…"
    return value


def normalize_field(cfg):
    """字段配置 -> (链式步骤 or None, 选项)。
    返回 None 表示普通 CSS selector 字段(走原有 bs4 逻辑, 向后兼容)。"""
    if isinstance(cfg, str):
        if re.match(r"^(css|xpath|regex):", cfg):
            return [cfg], {}
        return None, {"selector": cfg}
    if isinstance(cfg, list):
        return cfg, {}
    if isinstance(cfg, dict):
        if cfg.get("chain"):
            return as_list(cfg["chain"]), cfg
        return None, cfg
    raise ValueError(f"无法识别的字段配置: {cfg!r}")


def bs4_to_lxml(scope):
    return lxml_html.fromstring(str(scope))


def first_css(chain_cfg):
    """链式配置的第一步若是 CSS, 返回其选择器(可用作动态渲染的等待元素)。"""
    steps = as_list(chain_cfg)
    if steps and isinstance(steps[0], str):
        s = steps[0].strip()
        if s.startswith("css:"):
            return s[4:].strip()
        if not s.startswith(("xpath:", "regex:")):
            return s
    return None


def validate_sites(sites):
    """启动前校验: 把所有选择器/正则先编译一遍, 配置错误立刻报错,
    避免爬到很深的层级后才因一个笔误失败。"""
    errors = []

    def check_field(fname, fcfg, where):
        try:
            steps, opts = normalize_field(fcfg)
        except ValueError as exc:
            errors.append(f"{where}.{fname}: {exc}")
            return
        if steps is not None:
            try:
                compile_chain(steps)
            except (ValueError, re.error) as exc:
                errors.append(f"{where}.{fname}: {exc}")
            return
        sel = (opts or {}).get("selector")
        if sel:
            try:
                soupsieve.compile(str(sel))
            except Exception as exc:
                errors.append(f"{where}.{fname}: 无效 CSS 选择器 {sel!r}: {exc}")
        rx = (opts or {}).get("regex")
        if rx:
            try:
                re.compile(str(rx), re.S)
            except re.error as exc:
                errors.append(f"{where}.{fname}: 无效正则 {rx!r}: {exc}")

    for site in sites:
        name = site.get("name")
        if not name:
            errors.append("存在缺少 name 的站点 (name 用作输出文件名)")
            name = "<未命名>"
        if not [u for u in as_list(site.get("start_urls") or [site.get("start_url")]) if u]:
            errors.append(f"站点 {name}: 未配置 start_url/start_urls")

        if site.get("crawl"):
            crawl = site["crawl"]
            links = crawl.get("links")
            if links is not None:
                if isinstance(links, dict):
                    if not links.get("chain"):
                        errors.append(f"站点 {name} crawl.links: dict 写法必须包含 chain")
                        links = None
                    else:
                        links = links["chain"]
                if links is not None:
                    try:
                        compile_chain(links)
                    except (ValueError, re.error) as exc:
                        errors.append(f"站点 {name} crawl.links: {exc}")
            targets = crawl.get("targets") or {}
            if targets.get("item"):
                try:
                    compile_chain(targets["item"])
                except (ValueError, re.error) as exc:
                    errors.append(f"站点 {name} crawl.targets.item: {exc}")
            for fname, fcfg in (targets.get("fields") or {}).items():
                check_field(fname, fcfg, f"站点 {name} crawl.targets.fields")
            for key in ("allow", "deny"):
                for pat in as_list(crawl.get(key)):
                    try:
                        re.compile(pat)
                    except re.error as exc:
                        errors.append(f"站点 {name} crawl.{key}: 无效正则 {pat!r}: {exc}")
        elif site.get("levels"):
            for i, level in enumerate(site["levels"], 1):
                where = f"站点 {name} 第{i}级({level.get('name', '')})"
                url_field = level.get("url_field")
                if url_field and i > 1:
                    prev_fields = site["levels"][i - 2].get("fields") or {}
                    if url_field not in prev_fields:
                        errors.append(f"{where}: url_field {url_field!r} 不在上一级 fields 中")
                if level.get("item_selector"):
                    try:
                        soupsieve.compile(str(level["item_selector"]))
                    except Exception as exc:
                        errors.append(
                            f"{where} item_selector: 无效 CSS 选择器 "
                            f"{level['item_selector']!r}: {exc}")
                for fname, fcfg in (level.get("fields") or {}).items():
                    check_field(fname, fcfg, where)
        else:
            errors.append(f"站点 {name}: levels 与 crawl 至少配置一个")

    if errors:
        raise SystemExit("配置校验失败:\n  - " + "\n  - ".join(errors))


def extract_fields(scope, fields_cfg, base_url):
    """按 fields 配置从单个节点提取字段。

    简写:  title: "h3 a"                     -> 取文本 (普通 CSS)
    详写:  link: {selector, attr, html, all, regex,
                regex_group, max_length, default}
    链式:  title: "xpath://h3/a"
           title: ["css:.content", "xpath:./p[1]", "regex:[^.]+."]
           title: {chain: [...], all: true, url: true, ...}
    """
    out = {}
    lxml_scope = None  # 同一条目的多个链式字段共用一次转换
    for name, cfg in fields_cfg.items():
        steps, opts = normalize_field(cfg)
        if steps is not None:
            if lxml_scope is None:
                lxml_scope = bs4_to_lxml(scope)
            out[name] = chain_field_values(lxml_scope, steps, opts, base_url)
            continue
        if isinstance(cfg, str):
            cfg = {"selector": cfg}
        sel = cfg.get("selector")

        # all: true -> 匹配所有元素合并；否则取第一个
        if cfg.get("all") and sel:
            try:
                nodes = scope.select(sel)
            except Exception:
                nodes = []
            parts = [extract_value(n, cfg, base_url) for n in nodes]
            value = ", ".join(p for p in parts if p)
        else:
            node = None
            if sel:
                try:
                    node = scope.select_one(sel)
                except Exception:
                    node = None
            else:
                node = scope  # 不写 selector = 以当前节点自身为对象
            value = extract_value(node, cfg, base_url) if node is not None else ""

        if not value:
            value = cfg.get("default", "")
        rx = cfg.get("regex")
        if rx and value:
            m = re.search(rx, value, re.S)
            if m:
                try:
                    value = m.group(int(cfg.get("regex_group", 0)))
                except (IndexError, TypeError, ValueError):
                    value = m.group(0)
        max_len = cfg.get("max_length")
        if isinstance(max_len, int) and 0 < max_len < len(value):
            value = value[:max_len].rstrip() + "…"
        out[name] = value.strip() if isinstance(value, str) else value
    return out


def extract_value(node, cfg, base_url):
    attr = cfg.get("attr")
    if cfg.get("html"):
        value = node.decode_contents().strip()
    elif attr and attr != "text":
        value = node.get(attr) or ""
        if attr in ("href", "src") and value:
            value = urljoin(base_url, value.strip())
    else:
        value = node.get_text(" ", strip=True)
    return value


def field_selectors(level):
    """收集该层 fields 里出现的所有 CSS 选择器, 用于校验页面内容"""
    sels = []
    for cfg in (level.get("fields") or {}).values():
        sel = cfg.get("selector") if isinstance(cfg, dict) else cfg
        if sel:
            sels.append(sel)
    return sels


def page_has_content(html, item_sel, field_sels):
    """校验静态抓到的 HTML 是否已包含要提取的内容(判断是否需要 JS 渲染)。

    - 配了 item_selector: 能选中至少一个元素即算有内容
    - 否则: 任一字段选择器能选中元素即算有内容
    - 什么选择器都没配: 无法判断, 认为静态可用
    """
    soup = BeautifulSoup(html, "html.parser")
    try:
        if item_sel:
            return bool(soup.select(item_sel))
    except Exception:
        return True  # 选择器写错时交给正常流程报错
    for sel in field_sels:
        try:
            if soup.select_one(sel) is not None:
                return True
        except Exception:
            continue
    return not field_sels


def fetch_auto(url, level, site, fetcher, headers, prefer_dynamic):
    """render: auto —— 先静态抓取并用选择器校验, 拿不到目标内容再回退 Playwright。

    返回 (html, 是否使用了动态渲染)。
    """
    item_sel = level.get("item_selector")
    wait_for = level.get("wait_for")
    wait_timeout = level.get("wait_timeout", 15)
    settle = level.get("settle", site.get("settle", 0))

    def render_dynamic():
        dyn_wait = wait_for or item_sel  # 没配 wait_for 就等条目元素本身
        dyn_settle = settle
        if not dyn_wait and dyn_settle <= 0:
            dyn_settle = 1.5  # 连条目选择器都没有时, 给 JS 留点渲染时间
        return fetcher.fetch(url, render="dynamic", wait_for=dyn_wait,
                             wait_timeout=wait_timeout, settle=dyn_settle,
                             headers=headers, best_effort=True)

    if prefer_dynamic:
        # 该站点之前已回退过动态, 直接走动态, 省一次无效的静态请求
        return render_dynamic(), True

    try:
        html = fetcher.fetch(url, render="static", headers=headers)
        if page_has_content(html, item_sel, field_selectors(level)):
            return html, False
        log(f"  [自动] 静态页面未发现目标内容, 切换动态渲染: {url}")
    except Exception as exc:
        log(f"  [自动] 静态请求失败({exc}), 尝试动态渲染: {url}")
    return render_dynamic(), True


def crawl_site(site, fetcher):
    """按 levels 配置逐层抓取，返回树形结果（第一层 item 为根节点）。"""
    levels = site.get("levels") or []
    if not levels:
        raise ValueError("未配置 levels")

    start_urls = site.get("start_urls") or [site.get("start_url")]
    start_urls = [u for u in start_urls if u]
    if not start_urls:
        raise ValueError("未配置 start_url")

    visited = set()
    roots = []
    auto_prefers_dynamic = False  # auto 模式: 一旦某页需要动态渲染, 该站点后续页面直接走动态
    current = []  # 待处理: {url: 要抓的页面, attach_to: 子项挂到哪个节点(None=根)}
    for u in start_urls:
        u = urldefrag(u)[0]  # 去掉 #锚点, 避免同一页面被重复抓取
        if u and u not in visited:
            visited.add(u)
            current.append({"url": u, "attach_to": None})

    for idx, level in enumerate(levels):
        if not current:
            break
        nxt = levels[idx + 1] if idx + 1 < len(levels) else None
        nxt_url_field = (nxt or {}).get("url_field")
        render = level.get("render") or site.get("render") or "auto"
        item_sel = level.get("item_selector")
        fields_cfg = level.get("fields") or {}
        wait_for = level.get("wait_for")
        wait_timeout = level.get("wait_timeout", 15)
        settle = level.get("settle", site.get("settle", 0))
        limit = level.get("limit")
        headers = site.get("headers") or None
        processed = 0
        next_current = []

        for entry in current:
            url, parent = entry["url"], entry["attach_to"]
            try:
                if render == "auto":
                    html, used_dynamic = fetch_auto(url, level, site, fetcher,
                                                    headers, auto_prefers_dynamic)
                    if used_dynamic:
                        auto_prefers_dynamic = True
                else:
                    html = fetcher.fetch(url, render=render, wait_for=wait_for,
                                         wait_timeout=wait_timeout, settle=settle,
                                         headers=headers)
            except Exception as exc:
                log(f"  [错误] 第{idx + 1}级 抓取失败 {url}: {exc}")
                continue

            soup = BeautifulSoup(html, "html.parser")
            try:
                nodes = soup.select(item_sel) if item_sel else [soup]
            except Exception as exc:
                log(f"  [错误] 第{idx + 1}级 选择器无效 {item_sel!r}: {exc}")
                nodes = []
            if item_sel and not nodes:
                log(f"  [警告] 第{idx + 1}级 选择器未匹配到元素: {item_sel} ({url})")

            for node in nodes:
                if limit and processed >= limit:
                    break
                fields = extract_fields(node, fields_cfg, base_url=url)
                item = {"url": url, "fields": fields, "children": []}
                if parent is None:
                    roots.append(item)
                else:
                    parent["children"].append(item)
                processed += 1

                # 下一层入口：本层 url_field 字段的值作为下一层要抓的链接
                if nxt_url_field:
                    link = str(fields.get(nxt_url_field, "")).strip()
                    if link:
                        # 去掉 #锚点再入队去重, 与递归模式行为一致
                        child_url = urldefrag(urljoin(url, link))[0]
                        if child_url not in visited:
                            visited.add(child_url)
                            next_current.append({"url": child_url, "attach_to": item})

            if limit and processed >= limit:
                log(f"  [信息] 第{idx + 1}级 达到 limit={limit}，本层提前结束")
                break

        log(f"  第{idx + 1}级({level.get('name', '')}) 共提取 {processed} 项")
        current = next_current

    return roots


def crawl_site_recursive(site, fetcher):
    """递归抓取模式(site.crawl): 不预设层级。

    - 沿 links 链式选择器选出的链接不断深入(BFS), 直到没有新链接/达到上限
    - 每访问一页, 用 targets.item 选出目标节点(不配则整页一条), 逐条提取字段
    - 链接可按 allow/deny 正则与域名过滤; 已访问 URL 自动去重
    """
    cfg = site["crawl"]
    start_urls = [u for u in (site.get("start_urls") or [site.get("start_url")]) if u]
    if not start_urls:
        raise ValueError("未配置 start_url")

    links_cfg = cfg.get("links")
    if isinstance(links_cfg, (str, list)):
        links_cfg = {"chain": links_cfg}
    links_steps = compile_chain(links_cfg["chain"]) if links_cfg else None
    links_attr = (links_cfg or {}).get("attr", "href")

    targets = cfg.get("targets") or {}
    item_steps = compile_chain(targets["item"]) if targets.get("item") else None
    fields_cfg = targets.get("fields") or {}

    allow = [re.compile(p) for p in as_list(cfg.get("allow"))]
    deny = [re.compile(p) for p in as_list(cfg.get("deny"))]
    same_host = cfg.get("same_host", True)
    base_hosts = {urlsplit(u).netloc.lower() for u in start_urls}
    max_depth = int(cfg.get("max_depth", 3))
    max_pages = int(cfg.get("max_pages", 50))
    max_items = cfg.get("max_items")
    include_page_url = cfg.get("include_page_url", False)
    headers = site.get("headers") or None
    wait_timeout = cfg.get("wait_timeout", 15)
    settle = cfg.get("settle", site.get("settle", 0))
    render = site.get("render") or "auto"

    def link_ok(link):
        if not link.startswith(("http://", "https://")):
            return False
        if any(d.search(link) for d in deny):
            return False
        if allow and not any(a.search(link) for a in allow):
            return False
        if same_host and urlsplit(link).netloc.lower() not in base_hosts:
            return False
        return True

    def render_dynamic():
        wait = cfg.get("wait_for") or first_css(targets.get("item"))
        s = settle
        if not wait and s <= 0:
            s = 1.5  # 没有可等待的选择器时, 给 JS 一点渲染时间
        return fetcher.fetch(url, render="dynamic", wait_for=wait,
                             wait_timeout=wait_timeout, settle=s,
                             headers=headers, best_effort=True)

    def page_has_targets_or_links(root):
        """auto 模式校验: 静态 HTML 里已有目标节点或可跟随链接即算静态可用"""
        try:
            if item_steps is not None:
                if any(hasattr(x, "itertext") for x in run_steps(root, item_steps)):
                    return True
            elif fields_cfg and any(
                v for v in extract_crawl_fields(
                    root, list(fields_cfg.items()), url).values()
            ):
                return True
            if links_steps is not None:
                for link in finalize_values(run_steps(root, links_steps),
                                            base_url=url, attr=links_attr,
                                            absolute=True):
                    if link_ok(link):
                        return True
        except Exception:
            return False
        return False

    def extract_crawl_fields(scope, named_fields, page_url):
        fields = {}
        for fname, fcfg in named_fields:
            steps, opts = normalize_field(fcfg)
            if steps is None:
                sel = (opts or {}).get("selector")
                steps = [sel] if sel else []
            if steps:
                fields[fname] = chain_field_values(scope, steps, opts or {}, page_url)
            else:
                fields[fname] = (opts or {}).get("default", "")
        return fields

    named_fields = list(fields_cfg.items())
    visited = set()
    results = []
    queue = deque((u, 0) for u in start_urls)
    pages = 0
    prefer_dynamic = False

    while queue:
        url, depth = queue.popleft()
        norm = urldefrag(url)[0]
        if norm in visited:
            continue
        visited.add(norm)

        # ---- 抓取页面 (auto 模式: 先静态, 校验不过再动态) ----
        try:
            if render == "dynamic":
                html = fetcher.fetch(url, render="dynamic",
                                     wait_for=cfg.get("wait_for"),
                                     wait_timeout=wait_timeout, settle=settle,
                                     headers=headers)
            elif render == "auto" and prefer_dynamic:
                html = render_dynamic()
            elif render == "auto":
                try:
                    html = fetcher.fetch(url, render="static", headers=headers)
                    if not page_has_targets_or_links(lxml_html.fromstring(html)):
                        log(f"  [自动] 静态页面未发现目标内容, 切换动态渲染: {url}")
                        html, prefer_dynamic = render_dynamic(), True
                except Exception as exc:
                    log(f"  [自动] 静态请求失败({exc}), 尝试动态渲染: {url}")
                    html, prefer_dynamic = render_dynamic(), True
            else:
                html = fetcher.fetch(url, render="static", headers=headers)
        except Exception as exc:
            log(f"  [错误] 抓取失败 {url}: {exc}")
            continue
        pages += 1

        # ---- 解析并提取 ----
        try:
            root = lxml_html.fromstring(html)
        except Exception as exc:
            log(f"  [错误] 页面解析失败 {url}: {exc}")
            continue
        try:
            if item_steps is not None:
                nodes = [x for x in run_steps(root, item_steps)
                         if hasattr(x, "itertext")]
            else:
                nodes = [root]
            added = 0
            for node in nodes:
                if max_items and len(results) >= int(max_items):
                    break
                fields = extract_crawl_fields(node, named_fields, url)
                if include_page_url:
                    fields = {"_page": url, **fields}
                results.append({"url": url, "fields": fields, "children": []})
                added += 1

            new_links = 0
            if depth < max_depth and links_steps is not None:
                seen_now = set()
                for link in finalize_values(run_steps(root, links_steps),
                                            base_url=url, attr=links_attr,
                                            absolute=True):
                    n = urldefrag(link)[0]
                    if n and link_ok(link) and n not in visited and n not in seen_now:
                        seen_now.add(n)
                        queue.append((link, depth + 1))
                        new_links += 1
            log(f"  [深度{depth}] {url} -> 提取 {added} 条, 新链接 {new_links} 个")
        except Exception as exc:
            log(f"  [错误] 页面处理失败 {url}: {exc}")
            continue

        if pages >= max_pages:
            log(f"  [信息] 达到 max_pages={max_pages}, 停止抓取")
            break
        if max_items and len(results) >= int(max_items):
            log(f"  [信息] 达到 max_items={max_items}, 停止抓取")
            break

    log(f"  共访问 {pages} 页, 提取 {len(results)} 条记录")
    return results


def collapse(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def site_to_markdown(site, roots, run_time):
    start_urls = site.get("start_urls") or [site.get("start_url")]
    lines = [
        f"# {site.get('title') or site['name']}",
        "",
        f"> 来源: {' | '.join(u for u in start_urls if u)}  ",
        f"> 抓取时间: {run_time}",
        "",
    ]

    def render_item(item, depth):
        indent = "  " * depth
        for key, val in item.get("fields", {}).items():
            v = collapse(val)
            if v:
                lines.append(f"{indent}- **{key}**: {v}")
        for child in item.get("children", []):
            render_item(child, depth + 1)

    for item in roots:
        render_item(item, 0)
        lines.append("")
    return "\n".join(lines)


def site_to_txt(roots, separator=" | ", with_keys=False, inherit_fields=True):
    """树形结果拍平成纯文本: 每个叶子条目一行, 行内拼接自身及上层的字段值。

    - separator:      字段值之间的分隔符 (如 " | " 或 "\\t")
    - with_keys:      行内是否带字段名, 如 "author: Albert Einstein"
    - inherit_fields: 是否把上层条目的字段值拼进本行 (列表页字段 + 详情页字段 合成一行)
    """
    lines = []

    def walk(item, inherited):
        kv = [(k, collapse(v)) for k, v in item.get("fields", {}).items()]
        own = [f"{k}: {v}" if with_keys else v for k, v in kv if v]
        parts = (inherited + own) if inherit_fields else own
        children = item.get("children") or []
        if children:
            for child in children:
                walk(child, parts)
        elif parts:
            lines.append(separator.join(parts))

    for root in roots:
        walk(root, [])
    return "\n".join(lines) + "\n"


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\s]+', "-", str(name)).strip("-")
    return name or "site"


def load_gist_id(cfg):
    env_id = os.environ.get("GIST_ID", "").strip()
    if env_id:
        return env_id
    if os.path.isfile(GIST_ID_FILE):
        with open(GIST_ID_FILE, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved
    return str((cfg.get("gist") or {}).get("id", "")).strip()


def gist_stale_files(existing_names, site_names):
    """Gist 中已不对应任何启用站点的爬虫输出文件 (站点被移除/改名后残留)。
    只清理爬虫产出的扩展名, 手动放进 Gist 的其它文件不受影响。"""
    stale = []
    for fname in existing_names:
        stem, ext = os.path.splitext(fname)
        if ext in (".md", ".txt", ".json") and stem not in site_names:
            stale.append(fname)
    return stale


def push_to_gist(cfg, files, site_names):
    token = os.environ.get("GIST_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "未设置 GIST_TOKEN。请创建 classic Personal Access Token(勾选 gist 权限), "
            "并配置为仓库 Secret: Settings → Secrets and variables → Actions → GIST_TOKEN"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def create_gist():
        payload = {
            "description": (cfg.get("gist") or {}).get("description", "多站点抓取结果"),
            "public": False,
            "files": files,
        }
        r = requests.post("https://api.github.com/gists", headers=headers,
                          json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        with open(GIST_ID_FILE, "w", encoding="utf-8") as f:
            f.write(data["id"])
        log(f"已创建新 Gist: {data['html_url']} (ID 已写入 gist_id.txt)")

    gist_id = load_gist_id(cfg)
    if not gist_id:
        create_gist()
        return

    # 更新已有 Gist; Gist 被手动删除(404)时自动重建
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers,
                     timeout=30)
    if r.status_code == 404:
        log(f"[警告] Gist {gist_id} 不存在(可能已被手动删除), 自动重新创建。"
            "若 Secret 中配置了 GIST_ID, 请同步更新")
        create_gist()
        return
    r.raise_for_status()

    patch = dict(files)
    for fname in gist_stale_files((r.json().get("files") or {}).keys(), site_names):
        log(f"[信息] 删除 Gist 中已废弃的文件: {fname}")
        patch[fname] = {"content": None}

    r = requests.patch(f"https://api.github.com/gists/{gist_id}", headers=headers,
                       json={"files": patch}, timeout=30)
    if r.status_code == 404:  # 查询时还在、推送时被删的窗口期
        log("[警告] Gist 刚被删除, 自动重新创建")
        create_gist()
        return
    r.raise_for_status()
    log(f"已更新 Gist: {r.json()['html_url']}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="多站点多级页面抓取器")
    parser.add_argument("config", nargs="?", default=os.path.join(BASE_DIR, "config.yaml"),
                        help="配置文件路径 (默认: config.yaml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="不推送 Gist, 结果写入本地 output/ 目录")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = expand_env(yaml.safe_load(f) or {})

    sites = [s for s in (cfg.get("sites") or []) if s.get("enabled", True)]
    if not sites:
        raise SystemExit("config.yaml 中没有启用的站点")
    validate_sites(sites)

    tz_offset = cfg.get("timezone_offset", 8)
    run_time = (datetime.now(timezone.utc) + timedelta(hours=tz_offset)).strftime(
        "%Y-%m-%d %H:%M:%S")

    fetcher = Fetcher(timeout=cfg.get("timeout", 30), delay=cfg.get("delay", 1.0),
                      retry=cfg.get("retry", 2))
    files, ok, fail = {}, 0, 0
    site_names = {sanitize_filename(s["name"]) for s in sites}
    try:
        for site in sites:
            log(f"=== 站点: {site.get('name')} ===")
            try:
                if site.get("crawl"):
                    roots = crawl_site_recursive(site, fetcher)
                else:
                    roots = crawl_site(site, fetcher)
            except Exception as exc:
                log(f"[错误] 站点 {site['name']} 抓取失败: {exc}")
                fail += 1
                continue
            fmt = (site.get("output") or {}).get("format", "txt")
            if fmt == "json":
                content = json.dumps(roots, ensure_ascii=False, indent=2)
                ext = "json"
            elif fmt == "txt":
                out_cfg = site.get("output") or {}
                content = site_to_txt(
                    roots,
                    separator=out_cfg.get("separator", " | "),
                    with_keys=out_cfg.get("with_keys", False),
                    inherit_fields=out_cfg.get("inherit_fields", True),
                )
                ext = "txt"
            else:
                content = site_to_markdown(site, roots, run_time)
                ext = "md"
            filename = f"{sanitize_filename(site['name'])}.{ext}"
            files[filename] = {"content": content}
            if args.dry_run:
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                path = os.path.join(OUTPUT_DIR, filename)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                log(f"已写入 {path}")
            ok += 1
    finally:
        fetcher.close()

    if not files:
        raise SystemExit("没有任何站点产出结果")

    if args.dry_run:
        log(f"[dry-run] 跳过 Gist 推送。成功 {ok} 个站点, 失败 {fail} 个")
    else:
        push_to_gist(cfg, files, site_names)
        log(f"完成: 成功 {ok} 个站点, 失败 {fail} 个")


if __name__ == "__main__":
    main()
