ARGS ?=

.PHONY: install uninstall publish-static

install:
	bash deploy/install.sh $(ARGS)

uninstall:
	bash deploy/uninstall.sh $(ARGS)

publish-static:
	price-bot mirror-artifacts $(ARGS)
