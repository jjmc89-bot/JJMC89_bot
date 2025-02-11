"""
Process Categories for discussion working pages.

&params;
"""

from __future__ import annotations

import re
from collections.abc import Generator, Iterable
from contextlib import suppress
from itertools import chain
from typing import Any, Literal, TypedDict

import mwparserfromhell
import pywikibot
from mwparserfromhell.nodes import Node, Template, Text, Wikilink
from mwparserfromhell.wikicode import Wikicode
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator
from pywikibot.bot import ExistingPageBot, SingleSiteBot
from pywikibot.page import PageSourceType
from pywikibot.pagegenerators import GeneratorFactory, parameterHelp
from pywikibot.textlib import removeDisabledParts, replaceExcept
from pywikibot_extensions.page import Page, get_redirects


docuReplacements = {"&params;": parameterHelp}  # noqa: N816
CONFIG: Config
EXCEPTIONS = ("comment", "math", "nowiki", "pre", "source")
TEXTLINK_NAMESPACES = (118,)
TPL: dict[str, Iterable[str | pywikibot.Page]] = {
    "cfd": [
        "Cfd full",
        "Cfm full",
        "Cfm-speedy full",
        "Cfr full",
        "Cfr-speedy full",
    ],
    "old cfd": ["Old CfD"],
}


class TemplateConfig(BaseModel):
    template: Page = Field(validation_alias="title")
    params_re: re.Pattern[str] | None = Field(
        default=None,
        validation_alias="params",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("template", mode="before")
    @classmethod
    def transform_title(cls, title: str) -> Page:
        return Page.from_wikilink(title, pywikibot.Site(), 10)

    @field_validator("params_re", mode="before")
    @classmethod
    def transform_params(cls, expr: str | None) -> re.Pattern[str] | None:
        return re.compile(expr) if expr else None

    def __contains__(self, item: object) -> bool:
        return item in self.redirects

    @property
    def redirects(self) -> frozenset[Page]:
        return get_redirects(frozenset((self.template,)), 10)


CFG_KEYS = Literal["cfd", "update"]
CFG_VALUES = list[TemplateConfig]


class Config(RootModel[dict[CFG_KEYS, CFG_VALUES]]):

    def __getitem__(self, item: CFG_KEYS) -> CFG_VALUES:
        return self.root[item]

    def lookup_template(
        self,
        item: CFG_KEYS,
        template: Page,
    ) -> TemplateConfig | None:
        for v in self[item]:
            if template in v.redirects:
                return v
        return None

    def templates(self, item: CFG_KEYS) -> set[Page]:
        return set(chain.from_iterable(v.redirects for v in self[item]))


class BotOptions(TypedDict, total=False):
    old_cat: pywikibot.Category
    new_cats: list[pywikibot.Category]
    generator: Iterable[pywikibot.Page]
    site: pywikibot.site.BaseSite
    summary: str


class Instruction(TypedDict, total=False):
    mode: str
    bot_options: BotOptions
    cfd_page: CfdPage
    action: str
    noredirect: bool
    redirect: bool
    result: str


class LineResults(TypedDict):
    cfd_page: CfdPage | None
    new_cats: list[pywikibot.Category]
    old_cat: pywikibot.Category | None
    prefix: str
    suffix: str


class CfdBot(SingleSiteBot, ExistingPageBot):

    update_options = {
        "always": True,
        "new_cats": [],
        "old_cat": None,
        "summary": None,
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.opt.new_cats = sorted(self.opt.new_cats, reverse=True)

    def treat_templates(self, wikicode: Wikicode) -> None:
        new_cats = self.opt.new_cats
        if len(new_cats) != 1:
            return
        if not (
            CONFIG.templates("update") & set(self.current_page.itertemplates())
        ):
            return
        new_cat: str = new_cats[0].title(with_ns=False)
        for tpl in wikicode.ifilter_templates():
            try:
                template = Page.from_wikilink(tpl.name, self.site, 10)
            except ValueError:
                continue
            tpl_cfg = CONFIG.lookup_template("update", template)
            if tpl_cfg is None:
                continue
            for param in tpl.params:
                if not (
                    tpl_cfg.params_re is None
                    or tpl_cfg.params_re.fullmatch(param.name.strip())
                ):
                    continue
                try:
                    param_value = param.value.strip()
                    param_page = Page.from_wikilink(param_value, self.site, 14)
                    param_cat = pywikibot.Category(param_page)
                except (ValueError, pywikibot.exceptions.Error):
                    continue
                if param_cat == self.opt.old_cat:
                    param.value = new_cat  # type: ignore[assignment]

    def treat_wikilinks(
        self,
        wikicode: Wikicode,
        *,
        textlinks: bool = False,
    ) -> str:
        cats = []
        old_cat_link = None
        for wikilink in wikicode.ifilter_wikilinks():
            link_title = wikilink.title.strip()
            if link_title.startswith(":") != textlinks:
                continue
            if "{{" in link_title and self.current_page.namespace() == 14:
                link_title = self.site.expand_text(
                    text=link_title,
                    title=self.current_page.title(),
                )
            try:
                link_page = Page.from_wikilink(link_title, self.site)
                link_cat = pywikibot.Category(link_page)
            except (ValueError, pywikibot.exceptions.Error):
                continue
            cats.append(link_cat)
            if link_cat == self.opt.old_cat:
                old_cat_link = wikilink
        if not old_cat_link:
            pywikibot.log(
                f"{self.opt.old_cat!r} not directly in {self.current_page!r}"
            )
            return str(wikicode)
        new_cats = self.opt.new_cats
        if len(new_cats) == 1 and new_cats[0] not in cats:
            # Update the title to keep the sort key.
            prefix = ":" if textlinks else ""
            old_cat_link.title = f"{prefix}{new_cats[0].title()}"  # type: ignore[assignment]  # noqa: E501
            return str(wikicode)
        for cat in new_cats:
            if cat not in cats:
                wikicode.insert_after(
                    old_cat_link,
                    f"\n{cat.title(as_link=True, textlink=textlinks)}",
                )
        return replaceExcept(
            str(wikicode),
            re.compile(rf"\n?{re.escape(str(old_cat_link))}", re.M),
            "",
            EXCEPTIONS,
            site=self.site,
        )

    def treat_page(self) -> None:
        text = self.current_page.text
        wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
        self.treat_templates(wikicode)
        text = self.treat_wikilinks(wikicode)
        if self.current_page.namespace() in TEXTLINK_NAMESPACES:
            wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
            text = self.treat_wikilinks(wikicode, textlinks=True)
        if text == self.current_page.text:
            self.current_page.purge(forcelinkupdate=True)
        else:
            self.put_current(
                text,
                summary=self.opt.summary,
                asynchronous=False,
                nocreate=True,
            )


class CfdPage(Page):

    def __init__(self, source: PageSourceType, title: str = "") -> None:
        super().__init__(source, title)
        if not (
            self.title(with_ns=False).startswith("Categories for discussion/")
            and self.namespace() == 4
        ):
            raise ValueError(f"{self!r} is not a CFD page.")

    def find_discussion(self, category: pywikibot.Category) -> CfdPage:
        if self.section():
            return self
        text = removeDisabledParts(self.text, tags=EXCEPTIONS, site=self.site)
        wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
        for section in wikicode.get_sections(levels=[4]):
            heading = section.filter_headings()[0]
            heading_title = heading.title.strip()
            for node in heading.title.ifilter():
                if not isinstance(node, Text):
                    # Don't use headings with anything other than text.
                    discussion = self
                    break
            else:
                discussion = self.__class__.from_wikilink(
                    f"{self.title()}#{heading_title}", self.site
                )
                if category.title() == heading_title:
                    return discussion
            # Split approximately into close, nom, and others.
            parts = str(section).split("(UTC)")
            if len(parts) < 3:
                continue
            # Parse the nom for category links.
            nom = mwparserfromhell.parse(parts[1], skip_style_tags=True)
            for node in nom.ifilter():
                page = cat_from_node(node, self.site)
                if page and category == page:
                    return discussion
        return self

    def get_result_action(
        self, category: pywikibot.Category
    ) -> tuple[str, str]:
        result = action = ""
        if not self.section():
            return result, action
        text = removeDisabledParts(self.text, tags=EXCEPTIONS, site=self.site)
        wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
        for section in wikicode.get_sections(levels=[4]):
            heading = section.filter_headings()[0]
            if heading.title.strip() == self.section():
                break
        else:
            return result, action
        for line in str(section).splitlines():
            matches = re.findall(
                r"''The result of the discussion was:''\s+'''(.+?)'''", line
            )
            if matches:
                result = matches[0]
            line_wc = mwparserfromhell.parse(line, skip_style_tags=True)
            for node in line_wc.ifilter():
                page = cat_from_node(node, self.site)
                if page and category == page:
                    matches = re.findall(r"'''Propose (.+?)'''", line)
                    if matches:
                        action = matches[0]
                    break
        return result, action


class CFDWPage(Page):

    MODES = ("move", "merge", "empty", "retain")

    def __init__(self, source: PageSourceType, title: str = "") -> None:
        super().__init__(source, title)
        if not (
            self.title(with_ns=False).startswith(
                "Categories for discussion/Working"
            )
            and self.namespace() == 4
        ):
            raise ValueError(f"{self!r} is not a CFDW page.")
        self.mode: str | None = None
        self.instructions: list[Instruction] = []

    def parse(self) -> None:
        text = removeDisabledParts(self.text, tags=EXCEPTIONS, site=self.site)
        wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
        for section in wikicode.get_sections(flat=True, include_lead=False):
            heading = section.filter_headings()[0]
            section_title = str(heading.title).lower()
            for mode in self.MODES:
                if mode in section_title:
                    self.mode = mode
                    break
            else:
                continue
            try:
                self._parse_section(str(section))
            except (ValueError, pywikibot.exceptions.Error):
                pywikibot.exception()
        self._check_run()

    def _parse_section(self, section: str) -> None:
        cfd_page = None
        cfd_prefix = cfd_suffix = ""
        for line in section.splitlines():
            assert self.mode is not None  # for mypy
            instruction = Instruction(
                mode=self.mode,
                bot_options=BotOptions(),
            )
            line_results = self._parse_line(line)
            instruction["bot_options"]["old_cat"] = line_results["old_cat"]
            instruction["bot_options"]["new_cats"] = line_results["new_cats"]
            if line_results["cfd_page"]:
                cfd_prefix = line_results["prefix"]
                cfd_suffix = line_results["suffix"]
            cfd_page = line_results["cfd_page"] or cfd_page
            if not (cfd_page and instruction["bot_options"]["old_cat"]):
                continue
            prefix = f"{line_results['prefix']} {cfd_prefix}"
            suffix = line_results["suffix"] or cfd_suffix
            if "NO BOT" in prefix:
                pywikibot.log(f"Bot disabled for: {line}")
                continue
            cfd = cfd_page.find_discussion(line_results["old_cat"])
            instruction["cfd_page"] = cfd
            if self.mode == "merge":
                instruction["redirect"] = "REDIRECT" in prefix
                instruction["result"] = self.mode
                _, action = cfd.get_result_action(
                    instruction["bot_options"]["old_cat"]
                )
                instruction["action"] = action or "merging"
            elif self.mode == "move":
                instruction["noredirect"] = "REDIRECT" not in prefix
            elif self.mode == "retain":
                nc_matches = re.findall(
                    r"\b(no consensus) (?:for|to) (\w+)\b", suffix, flags=re.I
                )
                not_matches = re.findall(
                    r"\b(not )(\w+)\b", suffix, flags=re.I
                )
                if nc_matches:
                    result, action = nc_matches[0]
                elif not_matches:
                    result = "".join(not_matches[0])
                    action = re.sub(r"ed$", "e", not_matches[0][1])
                elif "keep" in suffix.lower():
                    result = "keep"
                    action = "delete"
                else:
                    result, action = cfd.get_result_action(
                        instruction["bot_options"]["old_cat"]
                    )
                instruction["result"] = result
                instruction["action"] = action
            self.instructions.append(instruction)

    def _parse_line(self, line: str) -> LineResults:
        results = LineResults(
            cfd_page=None,
            old_cat=None,
            new_cats=[],
            prefix="",
            suffix="",
        )
        link_found = False
        wikicode = mwparserfromhell.parse(line, skip_style_tags=True)
        nodes = wikicode.filter(recursive=False)
        for index, node in enumerate(nodes, start=1):
            if isinstance(node, Text):
                if not link_found:
                    results["prefix"] = str(node).strip()
                elif link_found and index == len(nodes):
                    results["suffix"] = str(node).strip()
            else:
                page = cat_from_node(node, self.site)
                if page:
                    link_found = True
                    if not results["old_cat"]:
                        results["old_cat"] = page
                    else:
                        results["new_cats"].append(page)
                elif isinstance(node, Wikilink):
                    link_found = True
                    page = CfdPage.from_wikilink(node, self.site)
                    results["cfd_page"] = page
        return results

    def _check_run(self) -> None:
        instructions = []
        seen = set()
        skip = set()
        # Collect categories and skips.
        for instruction in self.instructions:
            if instruction in instructions:
                # Remove duplicate.
                continue
            instructions.append(instruction)
            old_cat = instruction["bot_options"]["old_cat"]
            if old_cat in seen:
                skip.add(old_cat)
            seen.add(old_cat)
            for new_cat in instruction["bot_options"]["new_cats"]:
                seen.add(new_cat)
        # Only action instructions that shouldn't be skipped.
        self.instructions = []
        for instruction in instructions:
            old_cat = instruction["bot_options"]["old_cat"]
            cats = {old_cat}
            cats.update(instruction["bot_options"]["new_cats"])
            if cats & skip:
                pywikibot.error(
                    f"{old_cat!r} is involved in multiple instructions. "
                    f"Skipping: {instruction!r}."
                )
            elif any(c.isDisambig() for c in cats):
                pywikibot.error(
                    f"{instruction!r} involves a disambiguation. Skipping."
                )
            elif check_instruction(instruction):
                self.instructions.append(instruction)
                do_instruction(instruction)


def add_old_cfd(
    page: pywikibot.Page,
    cfd_page: CfdPage,
    action: str,
    result: str,
    summary: str,
) -> None:
    date = cfd_page.title(with_section=False).rpartition("/")[2]
    wikicode = mwparserfromhell.parse(page.text, skip_style_tags=True)
    for tpl in wikicode.ifilter_templates():
        try:
            template = Page.from_wikilink(tpl.name, page.site, 10)
        except ValueError:
            continue
        if template not in TPL["old cfd"] or not tpl.has(
            "date", ignore_empty=True
        ):
            continue
        if tpl.get("date").value.strip() == date:
            # Template already present.
            return
    wikicode.insert(0, "\n")
    old_cfd = Template("Old CfD")
    old_cfd.add("action", action)
    old_cfd.add("date", date)
    old_cfd.add("section", cfd_page.section())
    old_cfd.add("result", result)
    wikicode.insert(0, old_cfd)
    page.text = str(wikicode)
    page.save(summary=summary)


def cat_from_node(
    node: Node, site: pywikibot.site.BaseSite
) -> pywikibot.Category | None:
    with suppress(
        ValueError,
        pywikibot.exceptions.InvalidTitleError,
        pywikibot.exceptions.SiteDefinitionError,
    ):
        if isinstance(node, Template):
            tpl = Page.from_wikilink(node.name, site, 10)
            if tpl in CONFIG.templates("cfd") and node.has("1"):
                title = node.get("1").strip()
                page = Page.from_wikilink(title, site, 14)
                return pywikibot.Category(page)
        elif isinstance(node, Wikilink):
            title = str(node.title).split("#", maxsplit=1)[0]
            page = Page.from_wikilink(title, site)
            return pywikibot.Category(page)
    return None


def check_instruction(instruction: Instruction) -> bool:
    bot_options = instruction["bot_options"]
    old_cat = bot_options["old_cat"]
    new_cats = bot_options["new_cats"]
    if old_cat in new_cats:
        pywikibot.error(f"{old_cat!r} is also a {instruction['mode']} target.")
        return False
    if instruction["mode"] == "empty":
        if new_cats:
            pywikibot.error(f"empty mode has new categories for {old_cat!r}.")
            return False
    elif instruction["mode"] == "merge":
        if not new_cats:
            pywikibot.error(
                f"merge mode has no new categories for {old_cat!r}."
            )
            return False
        if not instruction["action"] or not instruction["result"]:
            pywikibot.error(f"Missing action or result for {old_cat!r}.")
            return False
        for new_cat in new_cats:
            if not new_cat.exists():
                pywikibot.error(f"{new_cat!r} does not exist.")
                return False
            if new_cat.isCategoryRedirect() or new_cat.isRedirectPage():
                pywikibot.error(f"{new_cat!r} is a redirect.")
                return False
    elif instruction["mode"] == "move":
        if len(new_cats) != 1:
            pywikibot.error(f"move mode has {len(new_cats)} new categories.")
            return False
        new_cat = new_cats[0]
        if (
            new_cat.exists()
            and old_cat.exists()
            and not old_cat.isCategoryRedirect()
        ):
            pywikibot.error(f"{new_cat!r} already exists.")
            return False
        if (
            old_cat.isCategoryRedirect() or old_cat.isRedirectPage()
        ) and not new_cat.exists():
            pywikibot.error(f"No target for move to {new_cats[0]!r}.")
            return False
        if new_cat.isCategoryRedirect() or new_cat.isRedirectPage():
            pywikibot.error(f"{new_cat!r} is a redirect.")
            return False
    elif instruction["mode"] == "retain":
        if not old_cat.exists():
            pywikibot.error(f"{old_cat!r} does not exist.")
            return False
        if new_cats:
            pywikibot.error(f"retain mode has new categories for {old_cat!r}.")
            return False
        if not instruction["action"] or not instruction["result"]:
            pywikibot.error(f"Missing action or result for {old_cat!r}.")
            return False
    else:
        pywikibot.error(f"Unknown mode: {instruction['mode']}.")
        return False
    return True


def delete_page(page: pywikibot.Page, summary: str) -> None:
    page.delete(
        reason=summary,
        prompt=False,
        deletetalk=page.toggleTalkPage().exists(),
    )
    if page.exists():
        return
    for redirect in page.redirects():
        redirect.delete(
            reason=(
                "[[WP:G8|G8]]: Redirect to deleted page "
                f"{page.title(as_link=True)}"
            ),
            prompt=False,
            deletetalk=redirect.toggleTalkPage().exists(),
        )


def do_instruction(instruction: Instruction) -> None:
    cfd_page = instruction["cfd_page"]
    bot_options = instruction["bot_options"]
    old_cat = bot_options["old_cat"]
    gen = chain(
        old_cat.members(), old_cat.backlinks(namespaces=TEXTLINK_NAMESPACES)
    )
    bot_options["generator"] = doc_page_add_generator(gen)
    bot_options["site"] = cfd_page.site
    cfd_link = cfd_page.title(as_link=True)
    if instruction["mode"] == "empty":
        bot_options["summary"] = (
            f"Removing {old_cat.title(as_link=True, textlink=True)} per "
            f"{cfd_link}"
        )
        CfdBot(**bot_options).run()
        # Wait for the category to be registered as empty.
        pywikibot.sleep(pywikibot.config.put_throttle)
        if old_cat.exists() and old_cat.isEmptyCategory():
            delete_page(old_cat, cfd_link)
    elif instruction["mode"] == "merge":
        redirect = False
        n_new_cats = len(bot_options["new_cats"])
        if n_new_cats == 1:
            new_cats = bot_options["new_cats"][0].title(
                as_link=True, textlink=True
            )
            redirect = instruction["redirect"]
        elif n_new_cats == 2:
            new_cats = " and ".join(
                cat.title(as_link=True, textlink=True)
                for cat in bot_options["new_cats"]
            )
        else:
            new_cats = f"{n_new_cats} categories"
        bot_options["summary"] = (
            f"Merging {old_cat.title(as_link=True, textlink=True)} to "
            f"{new_cats} per {cfd_link}"
        )
        CfdBot(**bot_options).run()
        # Wait for the category to be registered as empty.
        pywikibot.sleep(pywikibot.config.put_throttle)
        if (
            old_cat.exists()
            and old_cat.isEmptyCategory()
            and not old_cat.isCategoryRedirect()
        ):
            if redirect:
                redirect_cat(
                    old_cat,
                    bot_options["new_cats"][0],
                    f"Merged to {new_cats} per {cfd_link}",
                )
                add_old_cfd(
                    old_cat.toggleTalkPage(),
                    cfd_page,
                    instruction["action"],
                    instruction["result"],
                    f"{cfd_link} closed as {instruction['result']}",
                )
            else:
                delete_page(old_cat, cfd_link)
    elif instruction["mode"] == "move":
        with suppress(pywikibot.exceptions.Error):
            old_cat.move(
                bot_options["new_cats"][0].title(),
                reason=cfd_link,
                noredirect=instruction["noredirect"],
                movesubpages=False,
            )
            remove_cfd_tpl(bot_options["new_cats"][0], "Category moved")
        bot_options["summary"] = (
            f"Moving {old_cat.title(as_link=True, textlink=True)} to "
            f"{bot_options['new_cats'][0].title(as_link=True, textlink=True)}"
            f" per {cfd_link}"
        )
        CfdBot(**bot_options).run()
        if not instruction["noredirect"]:
            pywikibot.sleep(pywikibot.config.put_throttle)
            redirect_cat(
                old_cat,
                bot_options["new_cats"][0],
                "This category redirect should be kept",
            )
    elif instruction["mode"] == "retain":
        summary = f"{cfd_link} closed as {instruction['result']}"
        remove_cfd_tpl(old_cat, summary)
        add_old_cfd(
            old_cat.toggleTalkPage(),
            cfd_page,
            instruction["action"],
            instruction["result"],
            summary,
        )


def doc_page_add_generator(
    generator: Iterable[pywikibot.Page],
) -> Generator[pywikibot.Page]:
    for page in generator:
        yield page
        if not page.namespace().subpages:
            continue
        for subpage in page.site.doc_subpage:
            doc_page = pywikibot.Page(page.site, f"{page.title()}{subpage}")
            if doc_page.exists():
                yield doc_page


def get_template_pages(
    templates: Iterable[pywikibot.Page],
) -> set[pywikibot.Page]:
    pages = set()
    for template in templates:
        if template.isRedirectPage():
            template = template.getRedirectTarget()
        if not template.exists():
            continue
        pages.add(template)
        for tpl in template.redirects():
            pages.add(tpl)
    return pages


def load_config() -> None:
    site = pywikibot.Site()
    page = Page(site, f"{site.username()}/config/CFDW/templates.json", 2)
    global CONFIG
    CONFIG = Config.model_validate_json(page.text)


def redirect_cat(
    cat: pywikibot.Category, target: pywikibot.Category, summary: str
) -> None:
    tpl = Template("Category redirect")
    tpl.add("1", target.title())
    tpl.add("keep", "yes")
    cat.text = str(tpl)
    cat.save(summary=summary, minor=False)


def remove_cfd_tpl(page: pywikibot.Page, summary: str) -> None:
    text = re.sub(
        r"<!--\s*BEGIN CFD TEMPLATE\s*-->.*?"
        r"<!--\s*END CFD TEMPLATE\s*-->\n*",
        "",
        page.get(force=True),
        flags=re.I | re.M | re.S,
    )
    wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
    for tpl in wikicode.ifilter_templates():
        try:
            template = Page.from_wikilink(tpl.name, page.site, 10)
        except ValueError:
            continue
        if template in TPL["cfd"]:
            wikicode.remove(tpl)
    page.text = str(wikicode).strip()
    page.save(summary=summary)


def main(*args: str) -> int:
    local_args = pywikibot.handle_args(args)
    site = pywikibot.Site()
    site.login()
    gen_factory = GeneratorFactory(site)
    gen_factory.handle_args(local_args)
    for key, value in TPL.items():
        TPL[key] = get_template_pages(
            [pywikibot.Page(site, tpl, ns=10) for tpl in value]
        )
    load_config()
    for page in gen_factory.getCombinedGenerator():
        page = CFDWPage(page)
        if page.protection().get("edit", ("", ""))[0] == "sysop":
            page.parse()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
