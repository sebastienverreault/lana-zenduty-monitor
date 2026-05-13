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

        this._icon = new St.Icon({
            icon_name: 'emblem-ok-symbolic',
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._summaryItem = new PopupMenu.PopupMenuItem('LANA Zenduty: loading', {
            reactive: false,
        });
        this.menu.addMenuItem(this._summaryItem);

        this._incidentSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._incidentSection);

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

    _applyStatus(status) {
        const color = status.color || 'gray';
        const counts = status.counts || {};
        const triggered = counts.triggered || 0;
        const acknowledged = counts.acknowledged || 0;

        if (color === 'red') {
            this._icon.icon_name = 'dialog-warning-symbolic';
            this._icon.style = 'color: #e01b24;';
        } else if (color === 'yellow') {
            this._icon.icon_name = 'dialog-warning-symbolic';
            this._icon.style = 'color: #f6d32d;';
        } else if (color === 'green') {
            this._icon.icon_name = 'emblem-ok-symbolic';
            this._icon.style = 'color: #33d17a;';
        } else {
            this._icon.icon_name = 'dialog-question-symbolic';
            this._icon.style = 'color: #9a9996;';
        }

        const updated = status.updated_at || 'never';
        const error = status.error ? ` error: ${status.error}` : '';
        this._summaryItem.label.text =
            `Triggered: ${triggered}  Acknowledged: ${acknowledged}  Updated: ${updated}${error}`;

        this._incidentSection.removeAll();
        const incidents = status.incidents || [];
        if (incidents.length === 0) {
            this._incidentSection.addMenuItem(new PopupMenu.PopupMenuItem('No filtered open incidents', {
                reactive: false,
            }));
            return;
        }

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
