import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

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
        this.menu.addMenuItem(this._summaryItem);

        this._scheduleItem = new PopupMenu.PopupMenuItem('Schedule: unknown', {
            reactive: false,
        });
        this.menu.addMenuItem(this._scheduleItem);

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

    _scheduleText(schedule) {
        if (!schedule || schedule.enabled === false)
            return 'Schedule: disabled';
        const state = schedule.status || (
            schedule.on_call === true ? 'on' : schedule.on_call === false ? 'off' : 'unknown'
        );
        const summary = schedule.summary || '';
        return `Schedule: ${state}${summary ? ` · ${summary}` : ''}`;
    }

    _applyStatus(status) {
        const color = status.color || 'gray';
        const counts = status.counts || {};
        const triggered = counts.triggered || 0;
        const acknowledged = counts.acknowledged || 0;
        const dependabotReady = counts.dependabot_ready || 0;
        const dependabotOpen = counts.dependabot_open || 0;
        const concourseFailed = counts.concourse_failed || 0;
        const schedule = status.schedule || {};
        const scheduleText = this._scheduleText(schedule);

        this._setIconColor(color);

        const updated = status.updated_at || 'never';
        const error = status.error ? ` error: ${status.error}` : '';
        const zendutyError = status.zenduty_error ? ` zenduty error: ${status.zenduty_error}` : '';
        const dependabotError = status.dependabot_error ? ` dependabot error: ${status.dependabot_error}` : '';
        const concourseError = status.concourse_error ? ` concourse error: ${status.concourse_error}` : '';
        this._summaryItem.label.text =
            `Zenduty: ${triggered} triggered · ${acknowledged} acknowledged · ${scheduleText}  Updated: ${updated}${error}${zendutyError}${dependabotError}${concourseError}`;
        this._scheduleItem.label.text = scheduleText;

        this._incidentSection.removeAll();
        const incidents = status.incidents || [];
        if (incidents.length === 0) {
            const suppressed = counts.schedule_suppressed || 0;
            const emptyText = schedule.enabled !== false && schedule.on_call === false ?
                `No filtered open incidents · off schedule${suppressed ? ` (${suppressed} hidden)` : ''}` :
                'No filtered open incidents';
            this._incidentSection.addMenuItem(new PopupMenu.PopupMenuItem(emptyText, {
                reactive: false,
            }));
        } else {
            for (const incident of incidents.slice(0, 8)) {
                const number = incident.incident_number || '?';
                const state = incident.status || '?';
                const title = (incident.title || '').replace(/\s+/g, ' ').slice(0, 90);
                const row = new PopupMenu.PopupMenuItem(`#${number} ${state}: ${title}`);
                row.connect('activate', () => {
                    this._openPath(incident.triage_log_path || this._detailsPath);
                });
                this._incidentSection.addMenuItem(row);

                const created = incident.creation_date || 'unknown time';
                const triage = incident.triage_status || 'pending';
                this._incidentSection.addMenuItem(new PopupMenu.PopupMenuItem(
                    `  created ${created} · triage ${triage}`,
                    {reactive: false}
                ));
            }
        }

        this._dependabotSection.removeAll();
        const concourse = status.concourse || {};
        const failures = concourse.failures || [];
        const concourseSnooze = concourse.snooze || {};
        const snoozeText = concourseSnooze.active ? ` · snoozed until ${concourseSnooze.until}` : '';
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
                const summary = (failure.failure_summary || failure.log_summary || '')
                    .replace(/\s+/g, ' ')
                    .slice(0, 90);
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
                const title = (pr.title || '').replace(/\s+/g, ' ').slice(0, 90);
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
