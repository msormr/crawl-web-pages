# -*- coding: utf-8 -*-
"""crawler.py 纯逻辑单元测试 (不访问网络)。运行: python -m pytest tests/ -q"""

import re

import pytest

import crawler

LIST_HTML = """
<html><body>
  <div class="quote">
    <span class="text">"A quote"</span>
    <small class="author">张三</small>
    <a href="/author/zhang">link</a>
    <span class="tag">t1</span><span class="tag">t2</span>
  </div>
  <div class="quote">
    <span class="text">"Another"</span>
    <small class="author">李四</small>
    <a href="/author/zhang#top">link</a>
    <span class="tag">t3</span>
  </div>
</body></html>
"""


def lxml(html):
    from lxml import html as lxml_html
    return lxml_html.fromstring(html)


def bs4(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


class StubFetcher:
    """离线替身: 按预置映射返回页面, 记录所有请求 URL。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, **kw):
        self.calls.append(url)
        return self.pages[url]

    def close(self):
        pass


# ---------- 链式抽取引擎 ----------

def test_compile_step_kinds():
    from cssselect import GenericTranslator
    kind, expr = crawler.compile_step("css:.a b")
    assert kind == "xpath"
    assert expr == GenericTranslator().css_to_xpath(".a b")
    assert crawler.compile_step(".a")[0] == "xpath"  # 不带前缀按 CSS 处理
    assert crawler.compile_step("xpath://h1") == ("xpath", "//h1")
    kind, rx = crawler.compile_step(r"regex:\d+")
    assert kind == "regex"
    assert rx.search("a12b").group() == "12"
    with pytest.raises(ValueError):
        crawler.compile_step("css:")
    with pytest.raises(re.error):
        crawler.compile_step("regex:[unclosed")


def test_run_steps_chain():
    root = lxml("<div><p class='a'>x<b>b1</b></p><p>hello world</p></div>")
    steps = crawler.compile_chain(["css:p.a", "xpath:.//b"])
    assert crawler.run_steps(root, steps)[0].text == "b1"
    # regex 在字符串结果上继续匹配
    steps = crawler.compile_chain(["xpath://p[2]/text()", "regex:hel(..)"])
    assert crawler.run_steps(root, steps) == ["lo"]
    # regex 之后不能再接 css/xpath
    with pytest.raises(ValueError):
        crawler.run_steps(root, crawler.compile_chain(["regex:a", "css:p"]))


def test_run_steps_regex_group():
    root = lxml("<div><p>hello world</p></div>")
    steps = crawler.compile_chain(["css:p", "regex:^(hello)"])
    assert crawler.run_steps(root, steps) == ["hello"]


def test_finalize_values():
    root = lxml("<div><a href='/x'>link</a><p>  多  行 </p><p></p></div>")
    a = root.xpath(".//a")[0]
    p, empty = root.xpath(".//p")
    assert crawler.finalize_values([a], base_url="http://s/", attr="href") == ["/x"]
    assert crawler.finalize_values([a], base_url="http://s/",
                                   attr="href", absolute=True) == ["http://s/x"]
    assert crawler.finalize_values([p]) == ["多 行"]
    assert crawler.finalize_values([empty]) == []  # 空文本被跳过


def test_normalize_field():
    assert crawler.normalize_field("h3 a") == (None, {"selector": "h3 a"})
    assert crawler.normalize_field("xpath://h1") == (["xpath://h1"], {})
    assert crawler.normalize_field(["css:.a", "regex:b"]) == (["css:.a", "regex:b"], {})
    steps, opts = crawler.normalize_field({"chain": ["css:.a"], "all": True})
    assert steps == ["css:.a"] and opts["all"] is True
    steps, opts = crawler.normalize_field({"selector": ".a", "attr": "href"})
    assert steps is None and opts["selector"] == ".a"


def test_chain_field_values():
    root = lxml(LIST_HTML)
    q = root.xpath(".//div[@class='quote']")[0]
    # 三步链: CSS -> XPath 取属性 -> 正则截取
    v = crawler.chain_field_values(q, ["css:a", "xpath:./@href", "regex:/author/(.+)$"],
                                   {}, "http://s/")
    assert v == "zhang"
    # url: true 相对链接转绝对
    v = crawler.chain_field_values(q, ["css:a", "xpath:./@href"], {"url": True}, "http://s/")
    assert v == "http://s/author/zhang"
    # all 合并多个匹配
    v = crawler.chain_field_values(q, ["css:.tag"], {"all": True}, "http://s/")
    assert v == "t1, t2"
    # default
    assert crawler.chain_field_values(q, ["css:.none"], {}, "http://s/") == ""
    assert crawler.chain_field_values(q, ["css:.none"], {"default": "-"}, "http://s/") == "-"


def test_first_css():
    assert crawler.first_css("css:.a") == ".a"
    assert crawler.first_css(".a") == ".a"
    assert crawler.first_css("xpath://a") is None
    assert crawler.first_css(["css:.a", "regex:b"]) == ".a"
    assert crawler.first_css(None) is None


# ---------- 字段提取 (bs4 路径) ----------

def test_extract_fields_plain():
    scope = bs4(LIST_HTML).select_one(".quote")
    fields = crawler.extract_fields(scope, {
        "text": ".text",
        "link": {"selector": "a", "attr": "href"},
        "tags": {"selector": ".tag", "all": True},
        "missing": {"selector": ".none", "default": "N/A"},
        "short": {"selector": ".author", "max_length": 1},
    }, base_url="http://s/list")
    assert fields["text"] == '"A quote"'
    assert fields["link"] == "http://s/author/zhang"
    assert fields["tags"] == "t1, t2"
    assert fields["missing"] == "N/A"
    assert fields["short"] == "张…"


def test_extract_fields_regex_option():
    scope = bs4('<div><time datetime="2026-10-25T08:00:00">x</time></div>')
    fields = crawler.extract_fields(scope, {
        "d": {"selector": "time", "attr": "datetime", "regex": r"\d{4}-\d{2}-\d{2}"},
        "y": {"selector": "time", "attr": "datetime",
              "regex": r"(\d{4})-(\d{2})", "regex_group": 2},
    }, base_url="http://s/")
    assert fields["d"] == "2026-10-25"
    assert fields["y"] == "10"


def test_extract_fields_html():
    scope = bs4("<div><p class='c'>a<b>b</b></p></div>")
    fields = crawler.extract_fields(scope, {"h": {"selector": "p.c", "html": True}},
                                    base_url="")
    assert fields["h"] == "a<b>b</b>"


# ---------- 页面校验 / 输出 / 工具 ----------

def test_page_has_content():
    assert crawler.page_has_content(LIST_HTML, ".quote", []) is True
    assert crawler.page_has_content("<p>nothing</p>", ".quote", []) is False
    assert crawler.page_has_content("<p>x</p>", None, [".quote"]) is False
    assert crawler.page_has_content("<div class='quote'>x</div>", None, [".quote"]) is True
    assert crawler.page_has_content("<p>x</p>", None, []) is True  # 无选择器视为静态可用


def test_site_to_txt():
    tree = [
        {"fields": {"list_title": "列表A"}, "children": [
            {"fields": {"title": "T1", "extra": ""}, "children": []},
        ]},
        {"fields": {"list_title": "列表B"}, "children": [
            {"fields": {"title": "T2"}, "children": []},
        ]},
    ]
    assert crawler.site_to_txt(tree) == "列表A | T1\n列表B | T2\n"
    assert crawler.site_to_txt(tree, with_keys=True) == \
        "list_title: 列表A | title: T1\nlist_title: 列表B | title: T2\n"
    assert crawler.site_to_txt(tree, inherit_fields=False) == "T1\nT2\n"


def test_site_to_markdown():
    site = {"name": "s", "title": "示例", "start_url": "http://s/"}
    tree = [{"fields": {"a": "A"}, "children": [{"fields": {"b": "B"}, "children": []}]}]
    md = crawler.site_to_markdown(site, tree, "2026-01-01 00:00:00")
    assert "# 示例" in md
    assert "- **a**: A" in md
    assert "  - **b**: B" in md


def test_sanitize_filename():
    assert crawler.sanitize_filename("a b/c*d") == "a-b-c-d"
    assert crawler.sanitize_filename("   ") == "site"
    assert crawler.sanitize_filename("中文名") == "中文名"


def test_expand_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc")
    cfg = crawler.expand_env({
        "a": "${MY_TOKEN}",
        "b": ["x${MY_TOKEN}y"],
        "c": "$MY_TOKEN",       # 只支持 ${VAR} 写法, 裸 $VAR 不展开
        "d": "${UNSET_XYZ}",    # 未设置的保持原样
    })
    assert cfg == {"a": "abc", "b": ["xabcy"], "c": "$MY_TOKEN", "d": "${UNSET_XYZ}"}


def test_gist_stale_files():
    stale = crawler.gist_stale_files(
        ["quotes.txt", "quotes-js.txt", "notes.md", "data.org", "quotes-crawl.txt"],
        {"quotes", "quotes-js"},
    )
    # 已改名站点的残留文件 + 非站点名的同名扩展名文件会被清理; 其它扩展名不动
    assert stale == ["notes.md", "quotes-crawl.txt"]


# ---------- 配置校验 ----------

def test_validate_sites_ok():
    crawler.validate_sites([{
        "name": "s",
        "start_url": "http://s/",
        "levels": [
            {"item_selector": ".quote",
             "fields": {"link": {"selector": "a", "attr": "href"}}},
            {"url_field": "link",
             "fields": {"chain_f": ["css:.a", r"regex:\d+"],
                        "x": {"selector": ".b", "regex": r"(\d+)"}}},
        ],
    }])


def test_validate_sites_errors():
    with pytest.raises(SystemExit) as ei:
        crawler.validate_sites([{
            "name": "s",
            "start_url": "http://s/",
            "levels": [
                {"item_selector": ".quote",
                 "fields": {"link": {"selector": "a", "attr": "href"}}},
                {"url_field": "nope",
                 "fields": {"bad": ["css:[[[" , "regex:("]}},
            ],
        }])
    msg = str(ei.value)
    assert "url_field" in msg
    assert "无效" in msg


def test_validate_sites_missing():
    with pytest.raises(SystemExit) as ei:
        crawler.validate_sites([{"name": "s"}])
    msg = str(ei.value)
    assert "start_url" in msg
    assert "levels" in msg


# ---------- 抓取流程 (离线, 用 StubFetcher) ----------

def test_crawl_site_levels_dedup_anchor():
    """两条 quote 指向同一详情页 (一个链接带 #anchor), 详情页只抓一次。"""
    site = {
        "name": "t",
        "start_url": "http://t/list",
        "levels": [
            {"item_selector": ".quote",
             "fields": {"author": ".author", "link": {"selector": "a", "attr": "href"}}},
            {"url_field": "link", "fields": {"bio": ".bio"}},
        ],
    }
    fetcher = StubFetcher({
        "http://t/list": LIST_HTML,
        "http://t/author/zhang": "<html><body><div class='bio'>bio</div></body></html>",
    })
    roots = crawler.crawl_site(site, fetcher)
    assert fetcher.calls == ["http://t/list", "http://t/author/zhang"]
    assert len(roots) == 2
    assert len(roots[0]["children"]) == 1
    assert roots[0]["children"][0]["fields"]["bio"] == "bio"


def test_crawl_site_recursive():
    def page(text, nxt):
        link = f"<a class='next' href='/{nxt}'>n</a>" if nxt else ""
        return (f"<html><body><div class='quote'>"
                f"<span class='text'>{text}</span></div>{link}</body></html>")

    site = {
        "name": "t",
        "start_url": "http://t/1",
        "crawl": {
            "links": "css:a.next",
            "deny": ["/3"],
            "targets": {"item": "css:.quote", "fields": {"q": ".text"}},
            "max_pages": 10,
        },
    }
    fetcher = StubFetcher({
        "http://t/1": page("q1", 2),
        "http://t/2": page("q2", 3),
        "http://t/3": page("q3", None),
    })
    results = crawler.crawl_site_recursive(site, fetcher)
    assert [r["fields"]["q"] for r in results] == ["q1", "q2"]  # /3 被 deny
    assert fetcher.calls == ["http://t/1", "http://t/2"]
