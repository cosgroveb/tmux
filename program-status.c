/* $OpenBSD$ */

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
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION
 * OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
 * CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <sys/types.h>
#include <netinet/in.h>

#include <ctype.h>
#include <resolv.h>
#include <stdlib.h>
#include <string.h>

#include "tmux.h"

enum program_status_state {
	PROGRAM_STATUS_NONE = -1,
	PROGRAM_STATUS_IDLE,
	PROGRAM_STATUS_WORKING,
	PROGRAM_STATUS_DONE,
	PROGRAM_STATUS_BLOCKED,
	PROGRAM_STATUS_ERROR,
	PROGRAM_STATUS_CLEAR
};
static const char *program_status_states[] = {
	[PROGRAM_STATUS_IDLE] = "idle",
	[PROGRAM_STATUS_WORKING] = "working",
	[PROGRAM_STATUS_DONE] = "done",
	[PROGRAM_STATUS_BLOCKED] = "blocked",
	[PROGRAM_STATUS_ERROR] = "error",
	[PROGRAM_STATUS_CLEAR] = "clear"
};
static const char *program_status_kinds[] = {
	"permission", "question", "auth"
};

struct program_status_publication {
	u_int					 id;
	uint64_t				 revision;
	RB_ENTRY(program_status_publication)	 entry;
};

static int
program_status_cmp(struct program_status_publication *a,
    struct program_status_publication *b)
{
	return ((a->id > b->id) - (a->id < b->id));
}
RB_HEAD(program_status_publications, program_status_publication);
RB_GENERATE_STATIC(program_status_publications, program_status_publication,
    entry, program_status_cmp);

struct program_status_client {
	struct event				 timer;
	int					 pending;
	int					 supported;
	int					 disabled;
	int					 cleanup;
	int					 reset;
	u_int					 states;
	u_int					 kinds;
	struct program_status_publications	 published;
};

static int
program_status_index(const char *value, const char **list, size_t count)
{
	size_t	 i;

	for (i = 0; i < count; i++) {
		if (strcmp(value, list[i]) == 0)
			return (i);
	}
	return (-1);
}

static char *
program_status_trim(char *s)
{
	char	*end;

	while (isspace((u_char)*s))
		s++;
	end = s + strlen(s);
	while (end > s && isspace((u_char)end[-1]))
		*--end = '\0';
	return (s);
}

/* Return a valid pair, skipping malformed ones without changing the record. */
static int
program_status_pair(char *pair, char **key, char **value)
{
	char	*p;

	*key = program_status_trim(pair);
	p = strchr(*key, '=');
	if (p == NULL)
		return (0);
	*p++ = '\0';
	*key = program_status_trim(*key);
	*value = program_status_trim(p);
	if (**key == '\0')
		return (0);
	for (p = *key; *p != '\0'; p++) {
		if (*p < 'a' || *p > 'z')
			return (0);
	}
	if (strspn(*value, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
	    "0123456789_.,+/=-") != strlen(*value))
		return (0);
	return (1);
}

static void
program_status_forget(struct program_status_client *psc)
{
	struct program_status_publication	*pub, *next;

	RB_FOREACH_SAFE(pub, program_status_publications, &psc->published, next) {
		RB_REMOVE(program_status_publications, &psc->published, pub);
		free(pub);
	}
}

static void
program_status_timeout(__unused int fd, __unused short events, void *data)
{
	struct program_status_client	*psc = data;

	psc->pending = 0;
}

void
program_status_stop(struct tty *tty)
{
	struct program_status_client	*psc = tty->program_status;

	if (psc == NULL)
		return;
	tty_keys_reset_program_status(tty);
	evtimer_del(&psc->timer);
	/* Teardown is bounded; no queued report may follow the final clear. */
	tty_flush(tty);
	if (psc->cleanup)
		tty_raw(tty, "\033\\\033]7501;state=clear:id=tmux\033\\");
	program_status_forget(psc);
	free(psc);
	tty->program_status = NULL;
}

void
program_status_lost(struct tty *tty)
{
	if (tty->program_status != NULL)
		tty->program_status->reset = 1;
}

static u_int
program_status_list(char *value, const char **names, size_t count)
{
	char	*name;
	int	 index;
	u_int	 bits = 0;

	while ((name = strsep(&value, ",")) != NULL) {
		index = program_status_index(name, names, count);
		if (index != -1)
			bits |= 1U << index;
	}
	return (bits);
}

void
program_status_reply(struct tty *tty, const char *body)
{
	struct program_status_client	*psc = tty->program_status;
	char				*copy, *next, *pair, *key, *value;
	u_int				 states = (1U << PROGRAM_STATUS_CLEAR) - 1, kinds = 7;

	if (psc == NULL || !psc->pending)
		return;
	copy = next = xstrdup(body);
	pair = strsep(&next, ":");
	if (strcmp(program_status_trim(pair), "?") != 0)
		goto out;
	while ((pair = strsep(&next, ":")) != NULL) {
		if (!program_status_pair(pair, &key, &value))
			continue;
		if (strlen(key) > 16)
			goto out;
		if (strcmp(key, "states") == 0)
			states = program_status_list(value, program_status_states,
			    PROGRAM_STATUS_CLEAR);
		else if (strcmp(key, "kinds") == 0)
			kinds = program_status_list(value, program_status_kinds, 3);
	}
	psc->pending = 0;
	psc->supported = 1;
	psc->cleanup = 1;
	psc->reset = 1;
	psc->states = states;
	psc->kinds = kinds;
	evtimer_del(&psc->timer);
out:
	free(copy);
}

static void
program_status_remove(struct tty *tty, u_int id)
{
	char	 buffer[80];

	xsnprintf(buffer, sizeof buffer, "\033]7501;state=clear:id=tmux/p%u\033\\",
	    id);
	tty_puts(tty, buffer);
}

static void
program_status_publish(struct tty *tty, struct window_pane *wp)
{
	struct program_status		*ps = wp->program_status;
	struct program_status_client	*psc = tty->program_status;
	char				 buffer[4097], progress[32], encoded[2737];

	xsnprintf(buffer, sizeof buffer, "\033]7501;state=%s:id=tmux/p%u:app=%s",
	    ps->state, wp->id,
	    ps->app != NULL ? ps->app : "tmux");
	if (ps->msg != NULL) {
		strlcat(buffer, ":msg=", sizeof buffer);
		b64_ntop(ps->msg, strlen(ps->msg), encoded, sizeof encoded);
		strlcat(buffer, encoded, sizeof buffer);
	}
	if (ps->title != NULL) {
		strlcat(buffer, ":title=", sizeof buffer);
		b64_ntop(ps->title, strlen(ps->title), encoded, sizeof encoded);
		strlcat(buffer, encoded, sizeof buffer);
	}
	if (ps->kind != -1 && (psc->kinds & (1U << ps->kind))) {
		strlcat(buffer, ":kind=", sizeof buffer);
		strlcat(buffer, program_status_kinds[ps->kind], sizeof buffer);
	}
	if (ps->progress != -1) {
		xsnprintf(progress, sizeof progress, ":progress=%d", ps->progress);
		strlcat(buffer, progress, sizeof buffer);
	}
	strlcat(buffer, "\033\\", sizeof buffer);
	tty_puts(tty, buffer);
}

void
program_status_sync(struct client *c)
{
	struct tty				*tty = &c->tty;
	struct program_status_client		*psc = tty->program_status;
	struct program_status_publication	*pub, *next, find;
	struct window_pane			*wp;
	struct timeval				 tv = { .tv_sec = TTY_QUERY_TIMEOUT };

	if ((c->flags & CLIENT_CONTROL) || !(tty->flags & TTY_STARTED))
		return;
	if (c->session == NULL || (c->flags & (CLIENT_SUSPENDED|CLIENT_DEAD))) {
		program_status_stop(tty);
		return;
	}
	if (!options_get_number(global_options, "forward-program-status")) {
		if (psc == NULL)
			return;
		if (!psc->disabled) {
			tty_keys_reset_program_status(tty);
			evtimer_del(&psc->timer);
			psc->pending = 0;
			psc->disabled = 1;
			psc->reset = 1;
			program_status_forget(psc);
		}
		if (psc->cleanup && psc->reset && !(tty->flags & TTY_BLOCK)) {
			tty_puts(tty, "\033\\\033]7501;state=clear:id=tmux\033\\");
			psc->reset = 0;
		}
		return;
	}
	if (tty->flags & TTY_BLOCK)
		return;
	if (psc == NULL) {
		tty_keys_reset_program_status(tty);
		psc = tty->program_status = xcalloc(1, sizeof *psc);
		RB_INIT(&psc->published);
		evtimer_set(&psc->timer, program_status_timeout, psc);
		psc->disabled = 1;
	}
	if (psc->disabled) {
		tty_keys_reset_program_status(tty);
		if (psc->cleanup && psc->reset) {
			tty_puts(tty, "\033\\\033]7501;state=clear:id=tmux\033\\");
			psc->reset = 0;
		}
		psc->disabled = 0;
		psc->supported = 0;
		psc->pending = 1;
		tty_puts(tty, "\033]7501;?\033\\");
		evtimer_add(&psc->timer, &tv);
	}
	if (!psc->supported)
		return;
	if (psc->reset) {
		program_status_forget(psc);
		tty_puts(tty, "\033]7501;state=clear:id=tmux\033\\");
		psc->reset = 0;
	}
	RB_FOREACH_SAFE(pub, program_status_publications, &psc->published, next) {
		wp = window_pane_find_by_id(pub->id);
		if (wp != NULL && session_has(c->session, wp->window) &&
		    wp->program_status != NULL &&
		    (psc->states & (1U << program_status_index(wp->program_status->state,
		    program_status_states, PROGRAM_STATUS_CLEAR))))
			continue;
		program_status_remove(tty, pub->id);
		RB_REMOVE(program_status_publications, &psc->published, pub);
		free(pub);
	}
	RB_FOREACH(wp, window_pane_tree, &all_window_panes) {
		if (!session_has(c->session, wp->window) || wp->program_status == NULL ||
		    !(psc->states & (1U << program_status_index(wp->program_status->state,
		    program_status_states, PROGRAM_STATUS_CLEAR))))
			continue;
		find.id = wp->id;
		pub = RB_FIND(program_status_publications, &psc->published, &find);
		if (pub == NULL) {
			pub = xcalloc(1, sizeof *pub);
			pub->id = wp->id;
			RB_INSERT(program_status_publications, &psc->published, pub);
		} else if (pub->revision == wp->program_status_revision)
			continue;
		program_status_publish(tty, wp);
		pub->revision = wp->program_status_revision;
	}
}
