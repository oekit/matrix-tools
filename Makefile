.PHONY: audit fix-tables fix-agents fix-changelog fix-versions gen-diagram fix-diagram pr-flow gen-skill check-skills install-skills

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

pr-flow:
	python3 scripts/pr_flow.py --apply

gen-diagram:
	python3 scripts/gen_diagram.py

fix-diagram:
	python3 scripts/gen_diagram.py --push

gen-skill:
	python3 scripts/gen_skill.py --site

check-skills:
	python3 scripts/gen_skill.py --check && python3 scripts/gen_skill_selftest.py

install-skills:
	@mkdir -p $(HOME)/.zcode/skills
	ln -sfn $(CURDIR)/skills/oekit $(HOME)/.zcode/skills/oekit
	ln -sfn $(CURDIR)/skills/oekit-release $(HOME)/.zcode/skills/oekit-release
	ln -sfn $(CURDIR)/skills/oekit-sweep $(HOME)/.zcode/skills/oekit-sweep
	@echo "已软链 oekit / oekit-release / oekit-sweep → ~/.zcode/skills/（软链保单源；改动 gen_skill.py 后 make gen-skill 即全局生效）"





