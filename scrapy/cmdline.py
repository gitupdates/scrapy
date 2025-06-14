from __future__ import annotations

import argparse
import cProfile
import inspect
import os
import sys
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

import scrapy
from scrapy.commands import BaseRunSpiderCommand, ScrapyCommand, ScrapyHelpFormatter
from scrapy.crawler import AsyncCrawlerProcess, CrawlerProcess
from scrapy.exceptions import UsageError
from scrapy.utils.misc import walk_modules
from scrapy.utils.project import get_project_settings, inside_project
from scrapy.utils.python import garbage_collect
from scrapy.utils.reactor import _asyncio_reactor_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    # typing.ParamSpec requires Python 3.10
    from typing_extensions import ParamSpec

    from scrapy.settings import BaseSettings, Settings

    _P = ParamSpec("_P")


class ScrapyArgumentParser(argparse.ArgumentParser):
    def _parse_optional(
        self, arg_string: str
    ) -> tuple[argparse.Action | None, str, str | None] | None:
        # Support something like ‘-o -:json’, where ‘-:json’ is a value for
        # ‘-o’, not another parameter.
        if arg_string.startswith("-:"):
            return None

        return super()._parse_optional(arg_string)


def _iter_command_classes(module_name: str) -> Iterable[type[ScrapyCommand]]:
    # TODO: add `name` attribute to commands and merge this function with
    # scrapy.utils.spider.iter_spider_classes
    for module in walk_modules(module_name):
        for obj in vars(module).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, ScrapyCommand)
                and obj.__module__ == module.__name__
                and obj not in (ScrapyCommand, BaseRunSpiderCommand)
            ):
                yield obj


def _get_commands_from_module(module: str, inproject: bool) -> dict[str, ScrapyCommand]:
    d: dict[str, ScrapyCommand] = {}
    for cmd in _iter_command_classes(module):
        if inproject or not cmd.requires_project:
            cmdname = cmd.__module__.split(".")[-1]
            d[cmdname] = cmd()
    return d


def _get_commands_from_entry_points(
    inproject: bool, group: str = "scrapy.commands"
) -> dict[str, ScrapyCommand]:
    cmds: dict[str, ScrapyCommand] = {}
    if sys.version_info >= (3, 10):
        eps = entry_points(group=group)
    else:
        eps = entry_points().get(group, ())
    for entry_point in eps:
        obj = entry_point.load()
        if inspect.isclass(obj):
            cmds[entry_point.name] = obj()
        else:
            raise ValueError(f"Invalid entry point {entry_point.name}")
    return cmds


def _get_commands_dict(
    settings: BaseSettings, inproject: bool
) -> dict[str, ScrapyCommand]:
    cmds = _get_commands_from_module("scrapy.commands", inproject)
    cmds.update(_get_commands_from_entry_points(inproject))
    cmds_module = settings["COMMANDS_MODULE"]
    if cmds_module:
        cmds.update(_get_commands_from_module(cmds_module, inproject))
    return cmds


def _get_project_only_cmds(settings: BaseSettings) -> set[str]:
    return set(_get_commands_dict(settings, inproject=True)) - set(
        _get_commands_dict(settings, inproject=False)
    )


def _pop_command_name(argv: list[str]) -> str | None:
    for i in range(1, len(argv)):
        if not argv[i].startswith("-"):
            return argv.pop(i)
    return None


def _print_header(settings: BaseSettings, inproject: bool) -> None:
    version = scrapy.__version__
    if inproject:
        print(f"Scrapy {version} - active project: {settings['BOT_NAME']}\n")

    else:
        print(f"Scrapy {version} - no active project\n")


def _print_commands(settings: BaseSettings, inproject: bool) -> None:
    _print_header(settings, inproject)
    print("Usage:")
    print("  scrapy <command> [options] [args]\n")
    print("Available commands:")
    cmds = _get_commands_dict(settings, inproject)
    for cmdname, cmdclass in sorted(cmds.items()):
        print(f"  {cmdname:<13} {cmdclass.short_desc()}")
    if not inproject:
        print()
        print("  [ more ]      More commands available when run from project directory")
    print()
    print('Use "scrapy <command> -h" to see more info about a command')


def _print_unknown_command_msg(
    settings: BaseSettings, cmdname: str, inproject: bool
) -> None:
    proj_only_cmds = _get_project_only_cmds(settings)
    if cmdname in proj_only_cmds and not inproject:
        cmd_list = ", ".join(sorted(proj_only_cmds))
        print(
            f"The {cmdname} command is not available from this location.\n"
            f"These commands are only available from within a project: {cmd_list}.\n"
        )
    else:
        print(f"Unknown command: {cmdname}\n")


def _print_unknown_command(
    settings: BaseSettings, cmdname: str, inproject: bool
) -> None:
    _print_header(settings, inproject)
    _print_unknown_command_msg(settings, cmdname, inproject)
    print('Use "scrapy" to see available commands')


def _run_print_help(
    parser: argparse.ArgumentParser,
    func: Callable[_P, None],
    *a: _P.args,
    **kw: _P.kwargs,
) -> None:
    try:
        func(*a, **kw)
    except UsageError as e:
        if str(e):
            parser.error(str(e))
        if e.print_help:
            parser.print_help()
        sys.exit(2)


def execute(argv: list[str] | None = None, settings: Settings | None = None) -> None:
    if argv is None:
        argv = sys.argv

    if settings is None:
        settings = get_project_settings()
        # set EDITOR from environment if available
        try:
            editor = os.environ["EDITOR"]
        except KeyError:
            pass
        else:
            settings["EDITOR"] = editor

    inproject = inside_project()
    cmds = _get_commands_dict(settings, inproject)
    cmdname = _pop_command_name(argv)
    if not cmdname:
        _print_commands(settings, inproject)
        sys.exit(0)
    elif cmdname not in cmds:
        _print_unknown_command(settings, cmdname, inproject)
        sys.exit(2)

    cmd = cmds[cmdname]
    parser = ScrapyArgumentParser(
        formatter_class=ScrapyHelpFormatter,
        usage=f"scrapy {cmdname} {cmd.syntax()}",
        conflict_handler="resolve",
        description=cmd.long_desc(),
    )
    settings.setdict(cmd.default_settings, priority="command")
    cmd.settings = settings
    cmd.add_options(parser)
    opts, args = parser.parse_known_args(args=argv[1:])
    _run_print_help(parser, cmd.process_options, args, opts)

    if cmd.requires_crawler_process:
        if settings[
            "TWISTED_REACTOR"
        ] == _asyncio_reactor_path and not settings.getbool("FORCE_CRAWLER_PROCESS"):
            cmd.crawler_process = AsyncCrawlerProcess(settings)
        else:
            cmd.crawler_process = CrawlerProcess(settings)
    _run_print_help(parser, _run_command, cmd, args, opts)
    sys.exit(cmd.exitcode)


def _run_command(cmd: ScrapyCommand, args: list[str], opts: argparse.Namespace) -> None:
    if opts.profile:
        _run_command_profiled(cmd, args, opts)
    else:
        cmd.run(args, opts)


def _run_command_profiled(
    cmd: ScrapyCommand, args: list[str], opts: argparse.Namespace
) -> None:
    if opts.profile:
        sys.stderr.write(f"scrapy: writing cProfile stats to {opts.profile!r}\n")
    loc = locals()
    p = cProfile.Profile()
    p.runctx("cmd.run(args, opts)", globals(), loc)
    if opts.profile:
        p.dump_stats(opts.profile)


if __name__ == "__main__":
    try:
        execute()
    finally:
        # Twisted prints errors in DebugInfo.__del__, but PyPy does not run gc.collect() on exit:
        # http://doc.pypy.org/en/latest/cpython_differences.html
        # ?highlight=gc.collect#differences-related-to-garbage-collection-strategies
        garbage_collect()
