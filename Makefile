PREFIX ?= $(HOME)/.local
STATE_DIR ?= $(HOME)/.local/state/lana-zenduty-monitor
CONFIG_DIR ?= $(HOME)/.config/lana-zenduty-monitor
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user
GNOME_EXT_UUID := lana-zenduty-monitor@galoy.io
GNOME_EXT_DIR := $(HOME)/.local/share/gnome-shell/extensions/$(GNOME_EXT_UUID)

.PHONY: help init poll daemon triage enqueue-triage enqueue-fix enqueue-drua-triage enqueue-drua-fix work-triage work-fixes work-drua-triage work-drua-fixes worker test install-user-service enable-user-service disable-user-service install-gnome-extension enable-gnome-extension reload-gnome-extension status details clean-state

help:
	@printf '%s\n' \
		'Targets:' \
		'  make init                    Create config/state directories and default config' \
		'  make poll                    Run one monitor poll via Codex' \
		'  make daemon                  Run polling loop in foreground' \
		'  make triage INCIDENT_ID=...  Run Codex investigation for one incident' \
		'  make enqueue-triage INCIDENT=... Queue triage by incident id or number' \
		'  make enqueue-fix INCIDENT=... Queue fix by incident id or number' \
		'  make enqueue-drua-triage INCIDENT=... Queue Drua shadow triage' \
		'  make enqueue-drua-fix INCIDENT=... Queue Drua shadow fix' \
		'  make work-triage             Process one queued triage job' \
		'  make work-fixes              Process one queued fix job' \
		'  make work-drua-triage        Process one queued Drua shadow triage job' \
		'  make work-drua-fixes         Process one queued Drua shadow fix job' \
		'  make worker                  Run triage/fix worker loop' \
		'  make details                 Open generated details HTML page' \
		'  make test                    Compile Python and validate extension JSON' \
		'  make install-user-service    Install systemd user service/timer' \
		'  make enable-user-service     Enable and start systemd user timer' \
		'  make install-gnome-extension Install GNOME Shell extension files' \
		'  make enable-gnome-extension  Enable installed GNOME Shell extension' \
		'  make reload-gnome-extension  Install and reload GNOME Shell extension' \
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

enqueue-triage: init
	@test -n "$(INCIDENT)" || (echo 'Set INCIDENT=<zenduty unique_id_or_number>' >&2; exit 2)
	python3 -m lana_zenduty_monitor enqueue-triage "$(INCIDENT)" $(ARGS)

enqueue-fix: init
	@test -n "$(INCIDENT)" || (echo 'Set INCIDENT=<zenduty unique_id_or_number>' >&2; exit 2)
	python3 -m lana_zenduty_monitor enqueue-fix "$(INCIDENT)" $(ARGS)

work-triage: init
	python3 -m lana_zenduty_monitor work-triage

work-fixes: init
	python3 -m lana_zenduty_monitor work-fixes

enqueue-drua-triage: init
	@test -n "$(INCIDENT)" || (echo 'Set INCIDENT=<zenduty unique_id_or_number>' >&2; exit 2)
	python3 -m lana_zenduty_monitor enqueue-drua-triage "$(INCIDENT)" $(ARGS)

enqueue-drua-fix: init
	@test -n "$(INCIDENT)" || (echo 'Set INCIDENT=<zenduty unique_id_or_number>' >&2; exit 2)
	python3 -m lana_zenduty_monitor enqueue-drua-fix "$(INCIDENT)" $(ARGS)

work-drua-triage: init
	python3 -m lana_zenduty_monitor work-drua-triage

work-drua-fixes: init
	python3 -m lana_zenduty_monitor work-drua-fixes

worker: init
	python3 -m lana_zenduty_monitor worker

test:
	python3 -m compileall -q lana_zenduty_monitor
	python3 -m json.tool gnome-extension/metadata.json >/dev/null
	python3 -c 'import tomllib; tomllib.load(open("config.example.toml", "rb"))'

install-user-service: init
	mkdir -p "$(SYSTEMD_USER_DIR)"
	sed \
		-e 's|@REPO_DIR@|$(CURDIR)|g' \
		systemd/lana-zenduty-monitor.service.in > "$(SYSTEMD_USER_DIR)/lana-zenduty-monitor.service"
	sed \
		-e 's|@REPO_DIR@|$(CURDIR)|g' \
		systemd/lana-zenduty-triage-worker.service.in > "$(SYSTEMD_USER_DIR)/lana-zenduty-triage-worker.service"
	sed \
		-e 's|@REPO_DIR@|$(CURDIR)|g' \
		systemd/lana-zenduty-fix-worker.service.in > "$(SYSTEMD_USER_DIR)/lana-zenduty-fix-worker.service"
	sed \
		-e 's|@REPO_DIR@|$(CURDIR)|g' \
		systemd/lana-zenduty-drua-triage-worker.service.in > "$(SYSTEMD_USER_DIR)/lana-zenduty-drua-triage-worker.service"
	sed \
		-e 's|@REPO_DIR@|$(CURDIR)|g' \
		systemd/lana-zenduty-drua-fix-worker.service.in > "$(SYSTEMD_USER_DIR)/lana-zenduty-drua-fix-worker.service"
	cp systemd/lana-zenduty-monitor.timer "$(SYSTEMD_USER_DIR)/lana-zenduty-monitor.timer"
	systemctl --user daemon-reload

enable-user-service: install-user-service
	systemctl --user enable --now lana-zenduty-monitor.timer
	systemctl --user enable --now lana-zenduty-triage-worker.service
	systemctl --user enable --now lana-zenduty-fix-worker.service
	systemctl --user enable --now lana-zenduty-drua-triage-worker.service
	systemctl --user enable --now lana-zenduty-drua-fix-worker.service

disable-user-service:
	systemctl --user disable --now lana-zenduty-monitor.timer || true
	systemctl --user disable --now lana-zenduty-triage-worker.service || true
	systemctl --user disable --now lana-zenduty-fix-worker.service || true
	systemctl --user disable --now lana-zenduty-drua-triage-worker.service || true
	systemctl --user disable --now lana-zenduty-drua-fix-worker.service || true

install-gnome-extension:
	mkdir -p "$(GNOME_EXT_DIR)"
	cp gnome-extension/metadata.json gnome-extension/extension.js gnome-extension/zenduty*.svg "$(GNOME_EXT_DIR)/"

enable-gnome-extension: install-gnome-extension
	gnome-extensions enable "$(GNOME_EXT_UUID)"

reload-gnome-extension: install-gnome-extension
	gnome-extensions disable "$(GNOME_EXT_UUID)" || true
	gnome-extensions enable "$(GNOME_EXT_UUID)"

status:
	python3 -m lana_zenduty_monitor status

details: init
	xdg-open "$(STATE_DIR)/details.html"

clean-state:
	rm -rf "$(STATE_DIR)"
