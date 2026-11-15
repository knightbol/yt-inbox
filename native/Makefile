CC ?= cc
PKGS = libcurl sqlite3 ncursesw expat json-c
CPPFLAGS += -D_GNU_SOURCE -D_XOPEN_SOURCE_EXTENDED=1 $(shell pkg-config --cflags $(PKGS))
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -Wshadow -Wformat=2
LDLIBS += $(shell pkg-config --libs $(PKGS)) -lm
SOURCES = main.c util.c store.c feeds.c sync.c player.c tmux.c tui.c install.c
OBJECTS = $(SOURCES:%.c=build/%.o)

.PHONY: all clean test sanitize
all: build/yt-inbox-c build/playback.lua
build:
	mkdir -p $@
build/embed: embed.c | build
	$(CC) -std=c11 -Wall -Wextra -O2 $< -o $@
build/schema.h: schema.sql build/embed
	build/embed schema_sql < $< > $@.tmp
	mv $@.tmp $@
build/%.o: %.c inbox.h build/schema.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -c $< -o $@
build/yt-inbox-c: $(OBJECTS)
	$(CC) $(LDFLAGS) $^ $(LDLIBS) -o $@
build/playback.lua: ../playback.lua | build
	cp $< $@
build/main-test.o: main.c inbox.h build/schema.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -DYTI_NO_MAIN -c $< -o $@
build/test-driver: tests/driver.c build/main-test.o $(filter-out build/main.o,$(OBJECTS))
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -I. $^ -Wl,--wrap=http $(LDLIBS) -o $@
test: all build/test-driver
	python3 -m unittest discover -s tests -v
sanitize:
	$(MAKE) clean
	$(MAKE) CFLAGS='-O1 -g -std=c11 -Wall -Wextra -Wpedantic -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' test
clean:
	rm -rf build
-include $(OBJECTS:.o=.d)
