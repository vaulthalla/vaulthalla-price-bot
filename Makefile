ARGS ?=

.PHONY: install uninstall

install:
	bash deploy/install.sh $(ARGS)

uninstall:
	bash deploy/uninstall.sh $(ARGS)
