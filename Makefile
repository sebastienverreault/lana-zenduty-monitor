PREFIX ?= $(HOME)/.local
STATE_DIR ?= $(HOME)/.local/state/lana-zenduty-monitor
CONFIG_DIR ?= $(HOME)/.config/lana-zenduty-monitor
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user
GNOME_EXT_UUID := lana-zenduty-monitor@galoy.io
GNOME_EXT_DIR := $(HOME)/.local/share/gnome-shell/extensions/$(GNOME_EXT_UUID)

.PHONY: help init poll daemon triage test install-user-service enable-user-service disable-user-service install-gnome-extension enable-gnome-extension status details clean-state

help:
	@printf '%s\n' \
		'Targets:' \
		'  make init                    Create config/state directories and default config' \
		'  make poll                    Run one monitor poll via Codex' \
		'  make daemon                  Run polling loop in foreground' \
		'  make triage INCIDENT_ID=...  Run Codex investigation for one incident' \
		'  make details                 Open generated details HTML page' \
		'  make test                    Compile Python and validate extension JSON' \
		'  make install-user-service    Install systemd user service/timer' \
		'  make enable-user-service     Enable and start systemd user timer' \
		'  make install-gnome-extension Install GNOME Shell extension files' \
		'  make enable-gnome-extension  Enable installed GNOME Shell extension' \
		'  make status                  Show monitor status JSON'

init:
	mkdir -p "$(STATE_DIR)" "$(CONFIG_DIR)"
	test -f "$(CONFIG_DIR)/config.toml" || cp config.example.toml "$(CONFIG_DIR)/config.toml"
	python3 -m lana_zenduty_monitor status --write-default

poll: init
	python3 -m lana_zenduty_monitor poll

daemon: init
	python3 -m lana_zenduty_monitor daemon

triage: init
	@test -n "$(INCIDENT_ID)" || (echo 'Set INCIDENT_ID=<zenduty unique_id>' >&2; exit 2)
	python3 -m lana_zenduty_monitor triage "$(INCIDENT_ID)"

test:
	python3 -m compileall -q lana_zenduty_monitor
	python3 -m json.tool gnome-extension/metadata.json >/dev/null
	python3 -c 'import tomllib; tomllib.load(open("config.example.toml", "rb"))'

install-user-service: init
	mkdir -p "$(SYSTEMD_USER_DIR)"
	sed \
		-e 's|@REPO_DIR@|$(CURDIR)|g' \
		systemd/lana-zenduty-monitor.service.in > "$(SYSTEMD_USER_DIR)/lana-zenduty-monitor.service"
	cp systemd/lana-zenduty-monitor.timer "$(SYSTEMD_USER_DIR)/lana-zenduty-monitor.timer"
	systemctl --user daemon-reload

enable-user-service: install-user-service
	systemctl --user enable --now lana-zenduty-monitor.timer

disable-user-service:
	systemctl --user disable --now lana-zenduty-monitor.timer || true

install-gnome-extension:
	mkdir -p "$(GNOME_EXT_DIR)"
	cp gnome-extension/metadata.json gnome-extension/extension.js "$(GNOME_EXT_DIR)/"

enable-gnome-extension: install-gnome-extension
	gnome-extensions enable "$(GNOME_EXT_UUID)"

status:
	python3 -m lana_zenduty_monitor status

details: init
	xdg-open "$(STATE_DIR)/details.html"

clean-state:
	rm -rf "$(STATE_DIR)"
