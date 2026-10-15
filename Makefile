.PHONY: audit fix-tables fix-agents fix-changelog fix-versions

audit:
	python3 scripts/audit.py

fix-tables:
	python3 scripts/gen_tables.py --push

fix-agents:
	python3 scripts/gen_agents.py

fix-changelog:
	python3 scripts/gen_changelog.py --push

fix-versions:
	python3 scripts/refresh_versions.py --push
