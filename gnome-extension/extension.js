import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const MAX_PRIMARY_TEXT = 96;
const MAX_DETAIL_TEXT = 140;

const Indicator = GObject.registerClass(
class LanaZendutyIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'LANA Zenduty Monitor');
        this._extension = extension;
        this._statusPath = GLib.build_filenamev([
            GLib.get_home_dir(),
            '.local',
            'state',
            'lana-zenduty-monitor',
            'status.json',
        ]);
        this._detailsPath = GLib.build_filenamev([
            GLib.get_home_dir(),
            '.local',
            'state',
            'lana-zenduty-monitor',
            'details.html',
        ]);
        this._logsPath = GLib.build_filenamev([
            GLib.get_home_dir(),
            '.local',
            'state',
            'lana-zenduty-monitor',
            'logs',
        ]);
        this._repoPath = GLib.build_filenamev([
            GLib.get_home_dir(),
            'source',
            'repos',
            'lana-zenduty-monitor',
        ]);

        this._attentionOverlay = null;
        this._attentionEventId = 0;
        this._dismissedAttentionKey = null;
        this._lastStatus = null;
        this._useUtcTime = false;
        const extensionPath = this._extension.path ||
            (this._extension.dir ? this._extension.dir.get_path() : '.');
        this._zendutyIconPaths = {
            green: GLib.build_filenamev([extensionPath, 'zenduty-green.svg']),
            yellow: GLib.build_filenamev([extensionPath, 'zenduty-yellow.svg']),
            red: GLib.build_filenamev([extensionPath, 'zenduty-red.svg']),
            gray: GLib.build_filenamev([extensionPath, 'zenduty-gray.svg']),
        };

        this._icon = new St.Icon({
            gicon: new Gio.FileIcon({
                file: Gio.File.new_for_path(this._zendutyIconPaths.gray),
            }),
            icon_size: 18,
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._summaryItem = new PopupMenu.PopupMenuItem('LANA Zenduty: loading', {
            reactive: false,
        });
        this._configureWrappedItem(this._summaryItem);
        this.menu.addMenuItem(this._summaryItem);

        this._scheduleItem = new PopupMenu.PopupMenuItem('Schedule: unknown', {
            reactive: false,
        });
        this._configureWrappedItem(this._scheduleItem);
        this.menu.addMenuItem(this._scheduleItem);

        this._errorItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
        });
        this._configureWrappedItem(this._errorItem);
        this._errorItem.hide();
        this.menu.addMenuItem(this._errorItem);

        this._incidentSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._incidentSection);

        this._dependabotSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._dependabotSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const openDetails = new PopupMenu.PopupMenuItem('Open details');
        openDetails.connect('activate', () => this._openPath(this._detailsPath));
        this.menu.addMenuItem(openDetails);

        const openLogs = new PopupMenu.PopupMenuItem('Open logs');
        openLogs.connect('activate', () => this._openPath(this._logsPath));
        this.menu.addMenuItem(openLogs);

        const openStatus = new PopupMenu.PopupMenuItem('Open status file');
        openStatus.connect('activate', () => this._openPath(this._statusPath));
        this.menu.addMenuItem(openStatus);

        const refreshNow = new PopupMenu.PopupMenuItem('Refresh now');
        refreshNow.connect('activate', () => {
            GLib.spawn_command_line_async('systemctl --user start lana-zenduty-monitor.service');
        });
        this.menu.addMenuItem(refreshNow);

        const snoozeConcourse = new PopupMenu.PopupMenuItem('Snooze Concourse failures 1h');
        snoozeConcourse.connect('activate', () => {
            this._runMonitorCommand('snooze-concourse --minutes 60');
        });
        this.menu.addMenuItem(snoozeConcourse);

        const clearConcourseSnooze = new PopupMenu.PopupMenuItem('Clear Concourse snooze');
        clearConcourseSnooze.connect('activate', () => {
            this._runMonitorCommand('unsnooze-concourse');
        });
        this.menu.addMenuItem(clearConcourseSnooze);

        this._timeModeItem = new PopupMenu.PopupMenuItem('Show times in UTC');
        this._timeModeItem.connect('activate', () => {
            this._useUtcTime = !this._useUtcTime;
            this._timeModeItem.label.text = this._useUtcTime ? 'Show times in local time' : 'Show times in UTC';
            if (this._lastStatus)
                this._applyStatus(this._lastStatus);
        });
        this.menu.addMenuItem(this._timeModeItem);

        this._refresh();
        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 15, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    destroy() {
        if (this._timerId) {
            GLib.Source.remove(this._timerId);
            this._timerId = null;
        }
        this._hideAttentionOverlay();
        super.destroy();
    }

    _refresh() {
        const status = this._readStatus();
        this._applyStatus(status);
    }

    _readStatus() {
        try {
            const [ok, contents] = GLib.file_get_contents(this._statusPath);
            if (!ok)
                return {color: 'gray', error: 'status file not readable', counts: {}, incidents: []};
            const decoder = new TextDecoder('utf-8');
            return JSON.parse(decoder.decode(contents));
        } catch (error) {
            return {color: 'gray', error: String(error), counts: {}, incidents: []};
        }
    }

    _openPath(path) {
        GLib.spawn_command_line_async(`xdg-open ${GLib.shell_quote(path)}`);
    }

    _runMonitorCommand(command) {
        const shellCommand = [
            `cd ${GLib.shell_quote(this._repoPath)}`,
            'set -a',
            '[ ! -f .env ] || source .env',
            'set +a',
            `python3 -m lana_zenduty_monitor ${command}`,
        ].join('; ');
        GLib.spawn_command_line_async(`zsh -lc ${GLib.shell_quote(shellCommand)}`);
        GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            this._refresh();
            return GLib.SOURCE_REMOVE;
        });
    }

    _setIconColor(color) {
        const iconColor = color === 'red' || color === 'yellow' ? color :
            color === 'green' || color === 'blue' ? 'green' : 'gray';
        this._icon.gicon = new Gio.FileIcon({
            file: Gio.File.new_for_path(this._zendutyIconPaths[iconColor]),
        });
    }

    _configureWrappedItem(item) {
        item.label.clutter_text.set_line_wrap(true);
        item.label.clutter_text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR);
        item.label.clutter_text.set_ellipsize(Pango.EllipsizeMode.NONE);
        item.label.style = 'max-width: 900px;';
        item.label.x_expand = true;
    }

    _stateStyle(color) {
        const colors = {
            red: '#ff5c5c',
            yellow: '#f6d32d',
            blue: '#62a0ea',
            green: '#33d17a',
            gray: '#c0bfbc',
        };
        return `color: ${colors[color] || colors.gray}; max-width: 900px;`;
    }

    _shorten(text, maxLength = MAX_PRIMARY_TEXT) {
        const compact = String(text || '').replace(/\s+/g, ' ').trim();
        if (compact.length <= maxLength)
            return compact;
        return `${compact.slice(0, maxLength - 1).trimEnd()}…`;
    }

    _pad(value) {
        return String(value).padStart(2, '0');
    }

    _formatTime(value) {
        if (!value)
            return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime()))
            return String(value);
        if (this._useUtcTime)
            return date.toISOString().replace('.000Z', 'Z');
        const offset = -date.getTimezoneOffset();
        const sign = offset >= 0 ? '+' : '-';
        const offsetHours = this._pad(Math.floor(Math.abs(offset) / 60));
        const offsetMinutes = this._pad(Math.abs(offset) % 60);
        return [
            date.getFullYear(),
            '-',
            this._pad(date.getMonth() + 1),
            '-',
            this._pad(date.getDate()),
            ' ',
            this._pad(date.getHours()),
            ':',
            this._pad(date.getMinutes()),
            ' UTC',
            sign,
            offsetHours,
            ':',
            offsetMinutes,
        ].join('');
    }

    _scheduleState(schedule) {
        if (!schedule || schedule.enabled === false)
            return 'disabled';
        if (schedule.status)
            return schedule.status;
        return schedule.on_call === true ? 'on' : schedule.on_call === false ? 'off' : 'unknown';
    }

    _scheduleColor(schedule) {
        const state = this._scheduleState(schedule);
        if (state === 'on')
            return 'green';
        if (state === 'off')
            return 'yellow';
        return 'gray';
    }

    _scheduleText(schedule) {
        const state = this._scheduleState(schedule);
        const start = this._formatTime(schedule.window_start);
        const end = this._formatTime(schedule.window_end);
        const windowText = start && end ? `${start} to ${end}` :
            end ? `until ${end}` :
            start ? `from ${start}` : this._shorten(schedule.summary || '', MAX_DETAIL_TEXT);
        return `Schedule: ${state}${windowText ? ` · ${windowText}` : ''}`;
    }

    _errorText(status, schedule) {
        const errors = [];
        if (status.error)
            errors.push(`Monitor: ${status.error}`);
        if (status.zenduty_error)
            errors.push(`Zenduty: ${status.zenduty_error}`);
        if (schedule.error)
            errors.push(`Schedule: ${schedule.error}`);
        if (status.dependabot_error)
            errors.push(`Dependabot: ${status.dependabot_error}`);
        if (status.concourse_error)
            errors.push(`Concourse: ${status.concourse_error}`);
        return this._shorten(errors.join(' · '), MAX_DETAIL_TEXT);
    }

    _wrappedMenuItem(text, color = null) {
        const item = new PopupMenu.PopupMenuItem(this._shorten(text), {
            reactive: false,
        });
        this._configureWrappedItem(item);
        if (color)
            item.label.style = this._stateStyle(color);
        return item;
    }

    _applyStatus(status) {
        this._lastStatus = status;
        const color = status.color || 'gray';
        const counts = status.counts || {};
        const triggered = counts.triggered || 0;
        const acknowledged = counts.acknowledged || 0;
        const dependabotReady = counts.dependabot_ready || 0;
        const dependabotOpen = counts.dependabot_open || 0;
        const concourseFailed = counts.concourse_failed || 0;
        const schedule = status.schedule || {};
        const scheduleState = this._scheduleState(schedule);
        const scheduleText = this._scheduleText(schedule);
        const workflowBits = [];
        if (counts.triage_pending || counts.triage_running)
            workflowBits.push(`triage ${counts.triage_running || 0}/${counts.triage_pending || 0}`);
        if (counts.bug_possible || counts.fix_running)
            workflowBits.push(`bugs ${counts.bug_possible || 0}`);
        if (counts.review_required)
            workflowBits.push(`review ${counts.review_required}`);
        if (counts.drua_triage_pending || counts.drua_triage_running)
            workflowBits.push(`drua triage ${counts.drua_triage_running || 0}/${counts.drua_triage_pending || 0}`);
        if (counts.drua_fix_pending || counts.drua_fix_running)
            workflowBits.push(`drua fix ${counts.drua_fix_running || 0}/${counts.drua_fix_pending || 0}`);
        if (counts.drua_fix_succeeded)
            workflowBits.push(`drua fixed ${counts.drua_fix_succeeded}`);
        const workflowText = workflowBits.length ? ` · ${workflowBits.join(' · ')}` : '';

        this._setIconColor(color);

        this._summaryItem.label.text =
            `Zenduty: ${triggered} triggered · ${acknowledged} acknowledged · Schedule: ${scheduleState}${workflowText}`;
        this._summaryItem.label.style = this._stateStyle(color);
        this._scheduleItem.label.text = scheduleText;
        this._scheduleItem.label.style = this._stateStyle(this._scheduleColor(schedule));
        const errorText = this._errorText(status, schedule);
        if (errorText) {
            this._errorItem.label.text = `Error: ${errorText}`;
            this._errorItem.label.style = this._stateStyle('red');
            this._errorItem.show();
        } else {
            this._errorItem.hide();
        }

        this._incidentSection.removeAll();
        const incidents = status.incidents || [];
        if (incidents.length === 0) {
            const suppressed = counts.schedule_suppressed || 0;
            const emptyText = schedule.enabled !== false && schedule.on_call === false ?
                `No filtered open incidents · off schedule${suppressed ? ` (${suppressed} hidden)` : ''}` :
                'No filtered open incidents';
            this._incidentSection.addMenuItem(this._wrappedMenuItem(emptyText, 'gray'));
        } else {
            for (const incident of incidents.slice(0, 8)) {
                const number = incident.incident_number || '?';
                const state = incident.status || '?';
                const title = this._shorten(incident.title || '');
                const row = new PopupMenu.PopupMenuItem(`#${number} ${state}: ${title}`);
                row.connect('activate', () => {
                    this._openPath(incident.triage_log_path || this._detailsPath);
                });
                this._incidentSection.addMenuItem(row);

                const created = this._formatTime(incident.creation_date) || 'unknown time';
                const triage = incident.triage_status || 'pending';
                this._incidentSection.addMenuItem(new PopupMenu.PopupMenuItem(
                    `  created ${created} · triage ${triage}`,
                    {reactive: false}
                ));
            }
        }
        const tracked = status.tracked_incidents || [];
        if (tracked.length > 0) {
            this._incidentSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            this._incidentSection.addMenuItem(this._wrappedMenuItem('Workflow history', 'gray'));
            for (const incident of tracked.slice(0, 6)) {
                const number = incident.incident_number || '?';
                const state = incident.workflow_state || 'pending';
                const druaState = incident.drua_workflow_state ? ` · drua ${incident.drua_workflow_state}` : '';
                const classification = incident.classification ? ` · ${incident.classification}` : '';
                const row = new PopupMenu.PopupMenuItem(`#${number} ${state}${classification}${druaState}`);
                row.connect('activate', () => {
                    this._openPath(incident.fix_workspace || incident.drua_fix_workspace || incident.triage_log_path || incident.drua_triage_log_path || this._detailsPath);
                });
                this._incidentSection.addMenuItem(row);
            }
        }

        this._dependabotSection.removeAll();
        const concourse = status.concourse || {};
        const failures = concourse.failures || [];
        const concourseSnooze = concourse.snooze || {};
        const snoozeText = concourseSnooze.active ?
            ` · snoozed until ${this._formatTime(concourseSnooze.until) || concourseSnooze.until}` : '';
        this._dependabotSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._dependabotSection.addMenuItem(new PopupMenu.PopupMenuItem(
            `Concourse: ${failures.length} failed${snoozeText}`,
            {reactive: false}
        ));
        if (failures.length === 0) {
            this._dependabotSection.addMenuItem(new PopupMenu.PopupMenuItem('No failed Concourse jobs', {
                reactive: false,
            }));
        } else {
            for (const failure of failures.slice(0, 8)) {
                const pipeline = failure.pipeline || '?';
                const job = failure.job || '?';
                const state = failure.status || 'failed';
                const summary = this._shorten(failure.failure_summary || failure.log_summary || '');
                const row = new PopupMenu.PopupMenuItem(`${pipeline}/${job} ${state}: ${summary}`);
                row.connect('activate', () => {
                    this._openPath(failure.url || this._detailsPath);
                });
                this._dependabotSection.addMenuItem(row);
            }
        }

        const dependabot = status.dependabot || {};
        const prs = dependabot.prs || [];
        const readyPrs = prs.filter(pr => pr.ready_to_merge);
        this._dependabotSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._dependabotSection.addMenuItem(new PopupMenu.PopupMenuItem(
            `Dependabot: ${readyPrs.length} ready / ${prs.length} open`,
            {reactive: false}
        ));
        if (prs.length === 0) {
            this._dependabotSection.addMenuItem(new PopupMenu.PopupMenuItem('No open Dependabot PRs', {
                reactive: false,
            }));
        } else {
            for (const pr of prs.slice(0, 8)) {
                const counts = pr.check_counts || {};
                const number = pr.number || '?';
                const state = pr.ready_to_merge ? 'ready' : 'blocked';
                const title = this._shorten(pr.title || '');
                const row = new PopupMenu.PopupMenuItem(`#${number} ${state}: ${title}`);
                row.connect('activate', () => {
                    this._openPath(pr.url || this._detailsPath);
                });
                this._dependabotSection.addMenuItem(row);
                this._dependabotSection.addMenuItem(new PopupMenu.PopupMenuItem(
                    `  checks ${counts.success || 0} ok · ${counts.failure || 0} failed · ${counts.pending || 0} pending`,
                    {reactive: false}
                ));
            }
        }

        this._syncAttentionOverlay(status);
    }

    _attentionKey(status) {
        const counts = status.counts || {};
        const triggered = counts.triggered || 0;
        const acknowledged = counts.acknowledged || 0;
        const dependabotReady = counts.dependabot_ready || 0;
        const concourse = status.concourse || {};
        const concourseSnooze = concourse.snooze || {};
        const concourseFailed = concourseSnooze.active ? 0 : (counts.concourse_failed || 0);
        const error = status.error || status.zenduty_error || status.dependabot_error || status.concourse_error || '';
        if (!triggered && !acknowledged && !dependabotReady && !concourseFailed && !error)
            return null;
        return [
            status.color || 'gray',
            triggered,
            acknowledged,
            dependabotReady,
            concourseFailed,
            error,
            (status.incidents || []).map(incident => incident.unique_id || incident.incident_number).join(','),
            ((status.concourse || {}).failures || [])
                .map(failure => `${failure.pipeline}/${failure.job}/${failure.build_id}`)
                .join(','),
            ((status.dependabot || {}).prs || [])
                .filter(pr => pr.ready_to_merge)
                .map(pr => pr.number)
                .join(','),
        ].join('|');
    }

    _syncAttentionOverlay(status) {
        const key = this._attentionKey(status);
        if (!key) {
            this._dismissedAttentionKey = null;
            this._hideAttentionOverlay();
            return;
        }
        if (key === this._dismissedAttentionKey || this._attentionOverlay)
            return;
        this._showAttentionOverlay(key);
    }

    _showAttentionOverlay(key) {
        this._attentionOverlay = new St.Widget({
            reactive: true,
            style: 'background-color: rgba(224, 27, 36, 0.42);',
            x: 0,
            y: 0,
            width: global.stage.width,
            height: global.stage.height,
        });
        Main.uiGroup.add_child(this._attentionOverlay);
        this._attentionOverlay.set_position(0, 0);
        this._attentionOverlay.set_size(global.stage.width, global.stage.height);
        this._attentionEventId = global.stage.connect('captured-event', (_actor, event) => {
            const eventType = event.type();
            if (
                eventType === Clutter.EventType.KEY_PRESS ||
                eventType === Clutter.EventType.BUTTON_PRESS ||
                eventType === Clutter.EventType.MOTION ||
                eventType === Clutter.EventType.TOUCH_BEGIN
            ) {
                this._dismissedAttentionKey = key;
                this._hideAttentionOverlay();
            }
            return Clutter.EVENT_PROPAGATE;
        });
    }

    _hideAttentionOverlay() {
        if (this._attentionEventId) {
            global.stage.disconnect(this._attentionEventId);
            this._attentionEventId = 0;
        }
        if (this._attentionOverlay) {
            this._attentionOverlay.destroy();
            this._attentionOverlay = null;
        }
    }
});

export default class LanaZendutyMonitorExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}
