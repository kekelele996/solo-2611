"""工作台向本机技能提供的运行参数。凭据由独立的本机请求按用途领取。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from solo_auto.settings import Settings, SkillSettings

# 只迁移工作台原先没有对应配置项的参数。旧配置里的账号和密钥单独处理。
PROFILE_FIELDS = {
    'claude': {'baseUrl': 'base_url', 'image': 'image', 'launchSpacingSeconds': 'launch_spacing_seconds',
               'replTimeoutSeconds': 'repl_timeout_seconds', 'slotWaitSeconds': 'slot_wait_seconds',
               'terminalReadOnly': 'terminal_read_only', 'terminalActivate': 'terminal_activate'},
    'git': {'userName': 'git_name', 'userEmail': 'git_email'},
    'defaults': {'subDirection': 'sub_direction', 'harness': 'harness', 'os': 'os'},
    'github': {'proxyHttp': 'github_proxy', 'autoPush': 'auto_push'},
    'submission': {'localReviewScoreDelta': 'review_score_delta', 'localReviewMinSimilarity': 'review_similarity'},
}


def legacy_runtime(data: dict[str, Any]) -> SkillSettings:
    values = {field: data[group][key] for group, keys in PROFILE_FIELDS.items() for key, field in keys.items()
              if key in data.get(group, {})}
    if values.get('sub_direction') == '其他' and data.get('defaults', {}).get('domain') == '数据工程':
        values['sub_direction'] = '数仓与数据湖'
    return SkillSettings.model_validate(values)


def runtime_profile(settings: Settings, limit: int) -> dict[str, Any]:
    groups = {group: {key: getattr(settings.skill, field) for key, field in keys.items()}
              for group, keys in PROFILE_FIELDS.items()}
    execution = settings.execution
    groups['claude'].update(maxContainers=limit, timeoutSeconds=execution.run_timeout_seconds,
                            runMode=execution.run_mode, openTerminalOnRun=execution.open_terminal_on_run,
                            continueMax=execution.continue_max, startAttempts=execution.start_attempts,
                            silentSeconds=execution.silent_seconds, noProgressSeconds=execution.no_progress_seconds,
                            fatalHoldMinutes=execution.fatal_hold_minutes)
    platform = urlsplit(settings.solo2.base_url)
    groups['defaults']['domain'] = settings.solo2.locked_domain
    groups['submission'].update(lockedDomain=settings.solo2.locked_domain, server=f'{platform.scheme}://{platform.netloc}',
                                autoSubmit=settings.submission.auto_submit,
                                pollIntervalSeconds=min(300, settings.submission.poll_interval_seconds),
                                pollTimeoutSeconds=min(600, settings.submission.poll_timeout_seconds))
    return {'configVersion': 5, 'tasksDir': str(settings.tasks_dir), **groups}
