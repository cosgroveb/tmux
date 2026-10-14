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
	PROGRAM_STATUS_KIND,
	PROGRAM_STATUS_PROGRESS,
	PROGRAM_STATUS_APP,
	PROGRAM_STATUS_TITLE,
	PROGRAM_STATUS_MSG,
	PROGRAM_STATUS_FIELDS
};

static const char *program_status_keys[] = {
	"id", "state", "kind", "progress", "app", "title", "msg"
};
static const char *program_status_states[] = {
	"blocked", "done", "error", "idle", "working"
};

struct program_status_record {
	char				*values[PROGRAM_STATUS_FIELDS];
	TAILQ_ENTRY(program_status_record) entry;
};
TAILQ_HEAD(program_status_records, program_status_record);

struct program_status {
	struct program_status_records	 records;
	u_int				 count;
	uint64_t			 revision;
	char				*json;
	char				*states;
};

static void
program_status_record_free(struct program_status_record *record)
{
	u_int	 i;

	for (i = 0; i < PROGRAM_STATUS_FIELDS; i++)
		free(record->values[i]);
	free(record);
}

static struct program_status *
program_status_get(struct window_pane *wp)
{
	if (wp->program_status == NULL) {
		wp->program_status = xcalloc(1, sizeof *wp->program_status);
		TAILQ_INIT(&wp->program_status->records);
	}
	return (wp->program_status);
}

static void
program_status_changed(struct window_pane *wp)
{
	struct program_status	*ps = wp->program_status;

	ps->revision++;
	free(ps->json);
	ps->json = NULL;
	free(ps->states);
	ps->states = NULL;
	server_status_window(wp->window);
	server_redraw_window_borders(wp->window);
}

static int
program_status_identifier(const char *s, int path)
{
	size_t	 length = 0;

	for (; *s != '\0'; s++) {
		if (path && *s == '/') {
			if (length == 0)
				return (0);
			length = 0;
		} else {
			if (!((*s >= 'a' && *s <= 'z') ||
			    (*s >= 'A' && *s <= 'Z') ||
			    (*s >= '0' && *s <= '9') ||
			    strchr("_.+-", *s) != NULL))
				return (0);
			if (++length > 32)
				return (0);
		}
	}
	return (length != 0);
}

/* Check limits even on malformed or subsequently overwritten pairs. */
static int
program_status_limits(u_int field, const u_char *value, size_t len)
{
	size_t	 i, segment = 0, depth = 1;

	switch (field) {
	case PROGRAM_STATUS_MSG:
		return (len <= 2732);
	case PROGRAM_STATUS_TITLE:
		return (len <= 256);
	case PROGRAM_STATUS_APP:
		return (len <= 32);
	case PROGRAM_STATUS_ID:
		if (len > 128)
			return (0);
		for (i = 0; i < len; i++) {
			if (value[i] == '/') {
				segment = 0;
				if (++depth > 8)
					return (0);
			} else if (++segment > 32)
				return (0);
		}
		break;
	}
	return (1);
}

static char *
program_status_decode(const u_char *value, size_t len, size_t limit)
{
	char		 encoded[2737];
	u_char		*decoded;
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
	decoded = xmalloc(limit + 1);
	size = b64_pton(encoded, decoded, limit + 1);
	if (size == -1 || (size_t)size > limit)
		goto fail;
	for (i = 0; i < (size_t)size;) {
		wc = decoded[i++];
		if (wc >= 0x80) {
			more = utf8_open(&ud, wc);
			if (more != UTF8_MORE)
				goto fail;
			while (i < (size_t)size && more == UTF8_MORE)
				more = utf8_append(&ud, decoded[i++]);
			if (more != UTF8_DONE ||
			    utf8_towc(&ud, &wc) != UTF8_DONE)
				goto fail;
		}
		if (wc < 0x20 || (wc >= 0x7f && wc <= 0x9f) ||
		    (wc >= 0xd800 && wc <= 0xdfff) || wc > 0x10ffff)
			goto fail;
	}
	decoded[size] = '\0';
	return ((char *)decoded);

fail:
	free(decoded);
	return (NULL);
}

/* Parse with lengths so embedded NULs cannot hide malformed pairs. */
static struct program_status_record *
program_status_parse(const u_char *buf, size_t len)
{
	struct program_status_record	*record;
	const u_char			*end = buf + len, *next, *eq;
	const u_char			*key, *keyend, *value, *valueend;
	char				*copy, *s;
	size_t				 keylen, valuelen, i;
	u_int				 field;
	long long			 progress;
	const char			*errstr;

	record = xcalloc(1, sizeof *record);
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
			goto fail;
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
			goto fail;
		for (i = 0; i < valuelen; i++) {
			if (!((value[i] >= 'a' && value[i] <= 'z') ||
			    (value[i] >= 'A' && value[i] <= 'Z') ||
			    (value[i] >= '0' && value[i] <= '9') ||
			    (value[i] != '\0' &&
			    strchr("_.,+/=-", value[i]) != NULL)))
				break;
		}
		if (i != valuelen && field == PROGRAM_STATUS_ID)
			goto fail;
		if (i != valuelen)
			continue;
		if (field == PROGRAM_STATUS_MSG || field == PROGRAM_STATUS_TITLE) {
			copy = program_status_decode(value, valuelen,
			    field == PROGRAM_STATUS_MSG ? 2048 : 192);
			if (copy == NULL)
				goto fail;
		} else
			copy = xstrndup((const char *)value, valuelen);
		free(record->values[field]);
		record->values[field] = copy;
	}
	s = record->values[PROGRAM_STATUS_STATE];
	if (s == NULL)
		goto fail;
	for (i = 0; i < nitems(program_status_states); i++) {
		if (strcmp(s, program_status_states[i]) == 0)
			break;
	}
	if (i == nitems(program_status_states) && strcmp(s, "clear") != 0)
		goto fail;
	s = record->values[PROGRAM_STATUS_ID];
	if (s == NULL)
		record->values[PROGRAM_STATUS_ID] = xstrdup("");
	else if (!program_status_identifier(s, 1))
		goto fail;
	s = record->values[PROGRAM_STATUS_APP];
	if (s != NULL && !program_status_identifier(s, 0)) {
		free(s);
		record->values[PROGRAM_STATUS_APP] = NULL;
	}
	s = record->values[PROGRAM_STATUS_KIND];
	if (s != NULL && (strcmp(record->values[PROGRAM_STATUS_STATE],
	    "blocked") != 0 || (strcmp(s, "permission") != 0 &&
	    strcmp(s, "question") != 0 && strcmp(s, "auth") != 0))) {
		free(s);
		record->values[PROGRAM_STATUS_KIND] = NULL;
	}
	s = record->values[PROGRAM_STATUS_PROGRESS];
	if (s != NULL) {
		record->values[PROGRAM_STATUS_PROGRESS] = NULL;
		progress = strtonum(s, 0, 100, &errstr);
		if (errstr == NULL && strspn(s, "0123456789") == strlen(s) &&
		    (strcmp(record->values[PROGRAM_STATUS_STATE], "working") == 0 ||
		    strcmp(record->values[PROGRAM_STATUS_STATE], "blocked") == 0))
			xasprintf(&record->values[PROGRAM_STATUS_PROGRESS],
			    "%lld", progress);
		free(s);
	}
	return (record);

fail:
	program_status_record_free(record);
	return (NULL);
}

static int
program_status_descendant(const char *id, const char *parent)
{
	size_t	 len = strlen(parent);

	return (len == 0 || (strncmp(id, parent, len) == 0 &&
	    (id[len] == '\0' || id[len] == '/')));
}

static void
program_status_remove(struct program_status *ps,
    struct program_status_record *record)
{
	TAILQ_REMOVE(&ps->records, record, entry);
	ps->count--;
	program_status_record_free(record);
}

void
program_status_update(struct window_pane *wp, const u_char *buf, size_t len)
{
	struct program_status		*ps;
	struct program_status_record	*record, *old, *next;
	char				*a, *b;
	u_int				 i, count;
	int				 changed = 1;

	if ((record = program_status_parse(buf, len)) == NULL)
		return;
	ps = program_status_get(wp);
	if (strcmp(record->values[PROGRAM_STATUS_STATE], "clear") == 0) {
		count = ps->count;
		TAILQ_FOREACH_SAFE(old, &ps->records, entry, next) {
			if (program_status_descendant(old->values[PROGRAM_STATUS_ID],
			    record->values[PROGRAM_STATUS_ID]))
				program_status_remove(ps, old);
		}
		program_status_record_free(record);
		if (count != ps->count)
			program_status_changed(wp);
		return;
	}
	TAILQ_FOREACH(old, &ps->records, entry) {
		if (strcmp(old->values[PROGRAM_STATUS_ID],
		    record->values[PROGRAM_STATUS_ID]) == 0)
			break;
	}
	if (old != NULL) {
		for (i = 0; i < PROGRAM_STATUS_FIELDS; i++) {
			a = old->values[i];
			b = record->values[i];
			if ((a == NULL) != (b == NULL) ||
			    (a != NULL && strcmp(a, b) != 0))
				break;
		}
		changed = (i != PROGRAM_STATUS_FIELDS);
		program_status_remove(ps, old);
	} else if (ps->count == PROGRAM_STATUS_RECORDS)
		program_status_remove(ps, TAILQ_FIRST(&ps->records));
	TAILQ_INSERT_TAIL(&ps->records, record, entry);
	ps->count++;
	if (changed)
		program_status_changed(wp);
}

/* Process exit and shell prompts expire active records, not their subtrees. */
void
program_status_end(struct window_pane *wp)
{
	struct program_status		*ps = wp->program_status;
	struct program_status_record	*record, *next;
	const char			*state;
	u_int				 count;

	if (ps == NULL)
		return;
	count = ps->count;
	TAILQ_FOREACH_SAFE(record, &ps->records, entry, next) {
		state = record->values[PROGRAM_STATUS_STATE];
		if (strcmp(state, "working") == 0 || strcmp(state, "blocked") == 0)
			program_status_remove(ps, record);
	}
	if (count != ps->count)
		program_status_changed(wp);
}

void
program_status_reset(struct window_pane *wp)
{
	struct program_status	*ps = wp->program_status;

	if (ps == NULL || ps->count == 0)
		return;
	while (!TAILQ_EMPTY(&ps->records))
		program_status_remove(ps, TAILQ_FIRST(&ps->records));
	program_status_changed(wp);
}

void
program_status_free(struct window_pane *wp)
{
	struct program_status	*ps = wp->program_status;

	if (ps == NULL)
		return;
	while (!TAILQ_EMPTY(&ps->records))
		program_status_remove(ps, TAILQ_FIRST(&ps->records));
	free(ps->json);
	free(ps->states);
	free(ps);
}

uint64_t
program_status_revision(struct window_pane *wp)
{
	return (wp->program_status == NULL ? 0 : wp->program_status->revision);
}

static int
program_status_record_cmp(const void *a, const void *b)
{
	const struct program_status_record *const *ra = a, *const *rb = b;

	return (strcmp((*ra)->values[PROGRAM_STATUS_ID],
	    (*rb)->values[PROGRAM_STATUS_ID]));
}

static const char *
program_status_app(struct program_status *ps, struct program_status_record *record)
{
	struct program_status_record	*ancestor;
	const char			*app = NULL, *id;
	size_t				 nearest = 0, len;

	TAILQ_FOREACH(ancestor, &ps->records, entry) {
		if (ancestor->values[PROGRAM_STATUS_APP] == NULL)
			continue;
		id = ancestor->values[PROGRAM_STATUS_ID];
		len = strlen(id);
		if (len >= nearest && program_status_descendant(
		    record->values[PROGRAM_STATUS_ID], id)) {
			nearest = len;
			app = ancestor->values[PROGRAM_STATUS_APP];
		}
	}
	return (app);
}

/* All stored strings are validated UTF-8 without control characters. */
static void
program_status_json_string(struct evbuffer *buffer, const char *s)
{
	evbuffer_add(buffer, "\"", 1);
	for (; *s != '\0'; s++) {
		if (*s == '\\' || *s == '"')
			evbuffer_add(buffer, "\\", 1);
		evbuffer_add(buffer, s, 1);
	}
	evbuffer_add(buffer, "\"", 1);
}

char *
program_status_json(struct window_pane *wp)
{
	struct program_status		*ps = program_status_get(wp);
	struct program_status_record	*records[PROGRAM_STATUS_RECORDS], *record;
	struct evbuffer			*buffer;
	const char			*value;
	u_int				 i = 0, field;

	if (ps->json != NULL)
		return (xstrdup(ps->json));
	TAILQ_FOREACH(record, &ps->records, entry)
		records[i++] = record;
	qsort(records, ps->count, sizeof *records, program_status_record_cmp);
	if ((buffer = evbuffer_new()) == NULL)
		fatalx("out of memory");
	evbuffer_add_printf(buffer, "{\"schema\":1,\"pane_id\":\"%%%u\","
	    "\"revision\":\"%llu\",\"records\":[", wp->id,
	    (unsigned long long)ps->revision);
	for (i = 0; i < ps->count; i++) {
		record = records[i];
		evbuffer_add(buffer, i == 0 ? "{" : ",{", i == 0 ? 1 : 2);
		for (field = 0; field < PROGRAM_STATUS_FIELDS; field++) {
			value = record->values[field];
			if (value == NULL)
				continue;
			evbuffer_add_printf(buffer, "%s\"%s\":",
			    field == 0 ? "" : ",", program_status_keys[field]);
			if (field == PROGRAM_STATUS_PROGRESS)
				evbuffer_add(buffer, value, strlen(value));
			else
				program_status_json_string(buffer, value);
		}
		value = program_status_app(ps, record);
		if (value != NULL) {
			evbuffer_add_printf(buffer, ",\"effective_app\":");
			program_status_json_string(buffer, value);
		}
		evbuffer_add(buffer, "}", 1);
	}
	evbuffer_add(buffer, "]}", 2);
	ps->json = xstrndup(EVBUFFER_DATA(buffer), EVBUFFER_LENGTH(buffer));
	evbuffer_free(buffer);
	return (xstrdup(ps->json));
}

char *
program_status_state_names(struct window_pane *wp)
{
	struct program_status		*ps = program_status_get(wp);
	struct program_status_record	*record;
	char				 names[64] = "";
	u_int				 i;

	if (ps->states != NULL)
		return (xstrdup(ps->states));
	for (i = 0; i < nitems(program_status_states); i++) {
		TAILQ_FOREACH(record, &ps->records, entry) {
			if (strcmp(record->values[PROGRAM_STATUS_STATE],
			    program_status_states[i]) == 0)
				break;
		}
		if (record == NULL)
			continue;
		if (*names != '\0')
			strlcat(names, " ", sizeof names);
		strlcat(names, program_status_states[i], sizeof names);
	}
	ps->states = xstrdup(names);
	return (xstrdup(ps->states));
}
