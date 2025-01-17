import logging
import sys
from typing import TYPE_CHECKING, List, Optional, Tuple

import click

from cycode import __version__
from cycode.cli import code_scanner
from cycode.cli.auth.auth_command import authenticate
from cycode.cli.config import config
from cycode.cli.consts import ISSUE_DETECTED_STATUS_CODE, NO_ISSUES_STATUS_CODE
from cycode.cli.models import Severity
from cycode.cli.user_settings.configuration_manager import ConfigurationManager
from cycode.cli.user_settings.credentials_manager import CredentialsManager
from cycode.cli.user_settings.user_settings_commands import add_exclusions, set_credentials
from cycode.cli.utils import scan_utils
from cycode.cli.utils.progress_bar import get_progress_bar
from cycode.cli.utils.progress_bar import logger as progress_bar_logger
from cycode.cyclient import logger
from cycode.cyclient.cycode_client_base import CycodeClientBase
from cycode.cyclient.models import UserAgentOptionScheme
from cycode.cyclient.scan_config.scan_config_creator import create_scan_client

if TYPE_CHECKING:
    from cycode.cyclient.scan_client import ScanClient

CONTEXT = {}


@click.group(
    commands={
        'repository': code_scanner.scan_repository,
        'commit_history': code_scanner.scan_repository_commit_history,
        'path': code_scanner.scan_path,
        'pre_commit': code_scanner.pre_commit_scan,
        'pre_receive': code_scanner.pre_receive_scan,
    },
    short_help='Scan content for secrets/IaC/sca/SAST violations. '
    'You need to specify which scan type: ci/commit_history/path/repository/etc',
)
@click.option(
    '--scan-type',
    '-t',
    default='secret',
    help="""
              \b
              Specify the scan you wish to execute (secret/iac/sca),
              the default is secret
              """,
    type=click.Choice(config['scans']['supported_scans']),
)
@click.option(
    '--secret',
    default=None,
    help='Specify a Cycode client secret for this specific scan execution',
    type=str,
    required=False,
)
@click.option(
    '--client-id',
    default=None,
    help='Specify a Cycode client ID for this specific scan execution',
    type=str,
    required=False,
)
@click.option(
    '--show-secret', is_flag=True, default=False, help='Show secrets in plain text', type=bool, required=False
)
@click.option(
    '--soft-fail',
    is_flag=True,
    default=False,
    help='Run scan without failing, always return a non-error status code',
    type=bool,
    required=False,
)
@click.option(
    '--output',
    '-o',
    default=None,
    help="""
              \b
              Specify the results output (text/json/table),
              the default is text
              """,
    type=click.Choice(['text', 'json', 'table']),
)
@click.option(
    '--severity-threshold',
    default=None,
    help='Show only violations at the specified level or higher (supported for SCA scan type only).',
    type=click.Choice([e.name for e in Severity]),
    required=False,
)
@click.option(
    '--sca-scan',
    default=None,
    help='Specify the sca scan you wish to execute (package-vulnerabilities/license-compliance), the default is both',
    multiple=True,
    type=click.Choice(config['scans']['supported_sca_scans']),
)
@click.option(
    '--monitor',
    is_flag=True,
    default=False,
    help="When specified, the scan results will be recorded in the knowledge graph. "
    "Please note that when working in 'monitor' mode, the knowledge graph "
    "will not be updated as a result of SCM events (Push, Repo creation).(supported for SCA scan type only).",
    type=bool,
    required=False,
)
@click.option(
    '--report',
    is_flag=True,
    default=False,
    help='When specified, a violations report will be generated. '
    'A URL link to the report will be printed as an output to the command execution',
    type=bool,
    required=False,
)
@click.pass_context
def code_scan(
    context: click.Context,
    scan_type: str,
    secret: str,
    client_id: str,
    show_secret: bool,
    soft_fail: bool,
    output: str,
    severity_threshold: str,
    sca_scan: List[str],
    monitor: bool,
    report: bool,
) -> int:
    if show_secret:
        context.obj['show_secret'] = show_secret
    else:
        context.obj['show_secret'] = config['result_printer']['default']['show_secret']

    if soft_fail:
        context.obj['soft_fail'] = soft_fail
    else:
        context.obj['soft_fail'] = config['soft_fail']

    context.obj['scan_type'] = scan_type

    # save backward compatability with old style command
    if output is not None:
        context.obj['output'] = output
        if output == 'json':
            context.obj['no_progress_meter'] = True

    context.obj['client'] = get_cycode_client(client_id, secret, not context.obj['show_secret'])
    context.obj['severity_threshold'] = severity_threshold
    context.obj['monitor'] = monitor
    context.obj['report'] = report

    _sca_scan_to_context(context, sca_scan)

    context.obj['progress_bar'] = get_progress_bar(hidden=context.obj['no_progress_meter'])
    context.obj['progress_bar'].start()

    return 1


@code_scan.result_callback()
@click.pass_context
def finalize(context: click.Context, *_, **__) -> None:
    progress_bar = context.obj.get('progress_bar')
    if progress_bar:
        progress_bar.stop()

    if context.obj['soft_fail']:
        sys.exit(0)

    exit_code = NO_ISSUES_STATUS_CODE
    if _should_fail_scan(context):
        exit_code = ISSUE_DETECTED_STATUS_CODE

    sys.exit(exit_code)


@click.group(
    commands={'scan': code_scan, 'configure': set_credentials, 'ignore': add_exclusions, 'auth': authenticate},
    context_settings=CONTEXT,
)
@click.option(
    '--verbose',
    '-v',
    is_flag=True,
    default=False,
    help='Show detailed logs',
)
@click.option(
    '--no-progress-meter',
    is_flag=True,
    default=False,
    help='Do not show the progress meter',
)
@click.option(
    '--output',
    '-o',
    default='text',
    help='Specify the output (text/json/table), the default is text',
    type=click.Choice(['text', 'json', 'table']),
)
@click.option(
    '--user-agent',
    default=None,
    help='Characteristic JSON object that lets servers identify the application',
    type=str,
)
@click.version_option(__version__, prog_name='cycode')
@click.pass_context
def main_cli(
    context: click.Context, verbose: bool, no_progress_meter: bool, output: str, user_agent: Optional[str]
) -> None:
    context.ensure_object(dict)
    configuration_manager = ConfigurationManager()

    verbose = verbose or configuration_manager.get_verbose_flag()
    context.obj['verbose'] = verbose
    # TODO(MarshalX): rework setting the log level for loggers
    log_level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(log_level)
    progress_bar_logger.setLevel(log_level)

    context.obj['output'] = output
    if output == 'json':
        no_progress_meter = True

    context.obj['no_progress_meter'] = no_progress_meter

    if user_agent:
        user_agent_option = UserAgentOptionScheme().loads(user_agent)
        CycodeClientBase.enrich_user_agent(user_agent_option.user_agent_suffix)


def get_cycode_client(client_id: str, client_secret: str, hide_response_log: bool) -> 'ScanClient':
    if not client_id or not client_secret:
        client_id, client_secret = _get_configured_credentials()
        if not client_id:
            raise click.ClickException('Cycode client id needed.')
        if not client_secret:
            raise click.ClickException('Cycode client secret is needed.')

    return create_scan_client(client_id, client_secret, hide_response_log)


def _get_configured_credentials() -> Tuple[str, str]:
    credentials_manager = CredentialsManager()
    return credentials_manager.get_credentials()


def _should_fail_scan(context: click.Context) -> bool:
    return scan_utils.is_scan_failed(context)


def _sca_scan_to_context(context: click.Context, sca_scan_user_selected: List[str]) -> None:
    for sca_scan_option_selected in sca_scan_user_selected:
        context.obj[sca_scan_option_selected] = True


if __name__ == '__main__':
    main_cli()
