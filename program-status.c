/*
 * Copyright (c) 2026 Brian Cosgrove
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF MIND, USE, DATA OR PROFITS, WHETHER
 * IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING
 * OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <sys/types.h>

#include <netinet/in.h>

#include <ctype.h>
#include <resolv.h>
#include <stdlib.h>
#include <string.h>

#include "tmux.h"

/* OSC 7501 revision 0.4: https://www.superlogical.com/rex/docs/build/program-status */
#define PROGRAM_STATUS_RECORDS 256

enum program_status_field {
	PROGRAM_STATUS_ID,
	PROGRAM_STATUS_STATE,
	PROGRAM_STATUS_APP,
	PROGRAM_STATUS_TITLE,
	PROGRAM_STATUS_MSG,
	PROGRAM_STATUS_FIELDS
};

static const char *program_status_keys[] = {
	"id", "state", "app", "title", "msg"
};
static const char *program_status_states[] = {
	"blocked", "done", "error", "idle", "working", "clear"
};

struct program_status {
	struct program_status	*next;
	const char		*state;
	size_t			 size;
	char			 id[129];
	char			 data[];
};

/* Lengths are checked on every pair, before duplicate fields are replaced. */
static int
program_status_limits(u_int field, const u_char *value, size_t len)
{
	static const size_t limits[] = { 128, 4096, 32, 256, 2732 };
	size_t		    i, segment = 0, depth = 1;

	if (len > limits[field])
		return (0);
	if (field == PROGRAM_STATUS_ID) {
		for (i = 0; i < len; i++) {
			if (value[i] == '/') {
				segment = 0;
				if (++depth > 8)
					return (0);
			} else if (++segment > 32)
				return (0);
		}
	}
	return (1);
}

static int
program_status_identifier(const char *s)
{
	size_t	 len = strspn(s,
	    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.+-");

	if (s[len] == '/' && len != 0)
		return (program_status_identifier(s + len + 1));
	return (len != 0 && s[len] == '\0');
}

static int
program_status_text(const u_char *value, size_t len, size_t limit)
{
	char		 encoded[2737];
	u_char		 decoded[2049];
	struct utf8_data	 ud;
	enum utf8_state	 more;
	size_t		 i;
	int		 size;
	wchar_t		 wc;

	memcpy(encoded, value, len);
	if (memchr(value, '=', len) == NULL) {
		while (len % 4 != 0)
			encoded[len++] = '=';
	}
	encoded[len] = '\0';
	size = b64_pton(encoded, decoded, limit + 1);
	if (size == -1 || (size_t)size > limit)
		return (0);
	for (i = 0; i < (size_t)size;) {
		wc = decoded[i++];
		if (wc >= 0x80) {
			more = utf8_open(&ud, wc);
			if (more != UTF8_MORE)
				return (0);
			while (i < (size_t)size && more == UTF8_MORE)
				more = utf8_append(&ud, decoded[i++]);
			if (more != UTF8_DONE ||
			    utf8_towc(&ud, &wc) != UTF8_DONE)
				return (0);
		}
		if (wc < 0x20 || (wc >= 0x7f && wc <= 0x9f) ||
		    (wc >= 0xd800 && wc <= 0xdfff) || wc > 0x10ffff)
			return (0);
	}
	return (1);
}

/* Parse with lengths so embedded NULs cannot hide malformed pairs. */
static struct program_status *
program_status_parse(const u_char *buf, size_t len)
{
	struct program_status		*record;
	u_char				 copy[4096], *end, *next, *eq;
	const u_char			*raw = buf;
	char				*values[PROGRAM_STATUS_FIELDS] = { NULL };
	const u_char			*key, *keyend, *value;
	u_char				*valueend;
	char				*s;
	size_t				 keylen, valuelen, i;
	u_int				 field;

	memcpy(copy, buf, len);
	copy[len] = '\0';
	buf = copy;
	end = copy + len;
	while (buf < end) {
		next = memchr(buf, ':', end - buf);
		if (next == NULL)
			next = end;
		eq = memchr(buf, '=', next - buf);
		key = buf;
		keyend = eq != NULL ? eq : next;
		while (key < keyend && isspace(*key))
			key++;
		while (keyend > key && isspace(keyend[-1]))
			keyend--;
		keylen = keyend - key;
		buf = next == end ? end : next + 1;
		if (keylen > 16)
			return (NULL);
		if (eq == NULL || keylen == 0)
			continue;
		value = eq + 1;
		valueend = next;
		while (value < valueend && isspace(*value))
			value++;
		while (valueend > value && isspace(valueend[-1]))
			valueend--;
		valuelen = valueend - value;
		for (field = 0; field < PROGRAM_STATUS_FIELDS; field++) {
			if (strlen(program_status_keys[field]) == keylen &&
			    memcmp(program_status_keys[field], key, keylen) == 0)
				break;
		}
		if (field == PROGRAM_STATUS_FIELDS)
			continue;
		if (!program_status_limits(field, value, valuelen))
			return (NULL);
		*valueend = '\0';
		if (strspn((const char *)value,
		    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
		    "0123456789_.,+/=-") != valuelen) {
			if (field == PROGRAM_STATUS_ID)
				return (NULL);
			continue;
		}
		if ((field == PROGRAM_STATUS_MSG || field == PROGRAM_STATUS_TITLE) &&
		    !program_status_text(value, valuelen,
		    field == PROGRAM_STATUS_MSG ? 2048 : 192))
			return (NULL);
		values[field] = (char *)value;
	}
	s = values[PROGRAM_STATUS_STATE];
	if (s == NULL)
		return (NULL);
	for (i = 0; i < nitems(program_status_states); i++) {
		if (strcmp(s, program_status_states[i]) == 0)
			break;
	}
	if (i == nitems(program_status_states))
		return (NULL);
	s = values[PROGRAM_STATUS_ID];
	if (s != NULL && !program_status_identifier(s))
		return (NULL);
	record = xcalloc(1, sizeof *record + len);
	record->state = program_status_states[i];
	if (s != NULL)
		strlcpy(record->id, s, sizeof record->id);
	record->size = len;
	memcpy(record->data, raw, len);
	return (record);
}

static int
program_status_descendant(const char *id, const char *parent)
{
	size_t	 len = strlen(parent);

	return (len == 0 || (strncmp(id, parent, len) == 0 &&
	    (id[len] == '\0' || id[len] == '/')));
}

/* Replace one record, clear a subtree, or expire active records with NULL. */
static void
program_status_apply(struct window_pane *wp, struct program_status *record)
{
	struct program_status	*old, **link = &wp->program_status;
	const char		*state, *id;
	u_int			 count = 0;
	int			 remove, changed = 0;

	while ((old = *link) != NULL) {
		state = old->state;
		id = old->id;
		if (record == NULL)
			remove = strcmp(state, "working") == 0 ||
			    strcmp(state, "blocked") == 0;
		else if (strcmp(record->state, "clear") == 0)
			remove = program_status_descendant(id,
			    record->id);
		else
			remove = strcmp(id, record->id) == 0;
		if (remove) {
			changed = 1;
			*link = old->next;
			free(old);
		} else {
			count++;
			link = &old->next;
		}
	}
	if (record != NULL &&
	    strcmp(record->state, "clear") != 0) {
		changed = 1;
		*link = record;
		if (count == PROGRAM_STATUS_RECORDS) {
			old = wp->program_status;
			wp->program_status = old->next;
			free(old);
		}
	} else
		free(record);
	if (!changed)
		return;
	server_status_window(wp->window);
	server_redraw_window_borders(wp->window);
}

void
program_status_update(struct window_pane *wp, const u_char *buf, size_t len)
{
	struct program_status	*record = program_status_parse(buf, len);

	if (record != NULL)
		program_status_apply(wp, record);
}

void
program_status_end(struct window_pane *wp)
{
	program_status_apply(wp, NULL);
}

void
program_status_free(struct window_pane *wp)
{
	struct program_status	*record;

	while ((record = wp->program_status) != NULL) {
		wp->program_status = record->next;
		free(record);
	}
}

void
program_status_reset(struct window_pane *wp)
{
	if (wp->program_status == NULL)
		return;
	program_status_free(wp);
	server_status_window(wp->window);
	server_redraw_window_borders(wp->window);
}

char *
program_status_snapshot(struct window_pane *wp)
{
	struct program_status	*record;
	struct evbuffer		*buffer = evbuffer_new();
	char			*s;

	if (buffer == NULL)
		fatalx("out of memory");
	evbuffer_add_printf(buffer, "\033]7501;state=clear\007");
	for (record = wp->program_status; record != NULL; record = record->next) {
		evbuffer_add_printf(buffer, "\033]7501;");
		evbuffer_add(buffer, record->data, record->size);
		evbuffer_add_printf(buffer, "\007");
	}
	s = xreallocarray(NULL, 4, EVBUFFER_LENGTH(buffer) + 1);
	strvisx(s, EVBUFFER_DATA(buffer), EVBUFFER_LENGTH(buffer),
	    VIS_OCTAL|VIS_CSTYLE|VIS_TAB|VIS_NL);
	evbuffer_free(buffer);
	return (s);
}

char *
program_status_state_names(struct window_pane *wp)
{
	struct program_status	*record;
	char			 names[64] = "";
	u_int			 i;

	for (i = 0; i < nitems(program_status_states); i++) {
		for (record = wp->program_status; record != NULL; record = record->next) {
			if (strcmp(record->state,
			    program_status_states[i]) == 0)
				break;
		}
		if (record == NULL)
			continue;
		if (*names != '\0')
			strlcat(names, " ", sizeof names);
		strlcat(names, program_status_states[i], sizeof names);
	}
	return (xstrdup(names));
}
