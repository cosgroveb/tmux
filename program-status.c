/* $OpenBSD$ */

/*
 * Copyright (c) 2026 Brian Cosgrove
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is provided, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <sys/types.h>

#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "tmux.h"

/*
 * Transparent OSC 7501 forwarding. This is transport only: reports are
 * forwarded live, in arrival order, with no per-ID state, no cache and no
 * replay. The only retained state is bounded per-client bookkeeping (probe,
 * support, namespace ownership and an owed clear), plus the ID rewrite into
 * the fixed "tmux" namespace.
 */

/* ID limits, enforced before and after translation. */
#define PROGRAM_STATUS_ID_LIMIT 128
#define PROGRAM_STATUS_SEGMENT_LIMIT 32
#define PROGRAM_STATUS_LEVELS_LIMIT 8
/* Characters allowed in an application-supplied ID. */
#define PROGRAM_STATUS_ID_CHARS \
	"abcdefghijklmnopqrstuvwxyz" \
	"ABCDEFGHIJKLMNOPQRSTUVWXYZ" \
	"0123456789_.+-"

/* Probe and generated control emissions always use ST. */
#define PROGRAM_STATUS_PROBE "\033]7501;?\033\\"
#define PROGRAM_STATUS_CLEAR_NS "\033]7501;state=clear:id=tmux\033\\"

/*
 * Per-client transport state. Allocated at the first sync pass that sees
 * the option enabled, freed only at final client teardown (tty_close).
 * tty_stop_tty runs mid-life for suspend and server lock and must not
 * free it, so cleanup ownership and any owed reset survive them.
 */
struct program_status_client {
	struct event	 timer;
	int		 pending;	/* probe reply outstanding */
	int		 supported;	/* outer terminal confirmed support */
	int		 cleanup;	/* we own the outer "tmux" namespace */
	int		 disabled;	/* option off, or probe re-armed */
	int		 reset;		/* owed "tmux" subtree clear */
};

/* Trim leading and trailing whitespace in place. */
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

/*
 * Shared eligibility terms for every OSC 7501 emission path: a
 * terminal client with a live tty. Callers add their own scope,
 * ownership and membership terms.
 */
static int
program_status_client_active(struct client *c)
{
	if (c->flags & CLIENT_CONTROL)
		return (0);
	if (c->flags & CLIENT_UNATTACHEDFLAGS)
		return (0);
	if (~c->tty.flags & TTY_STARTED)
		return (0);
	return (1);
}

/*
 * Eligibility for lifecycle clears: client-scoped terms plus namespace
 * ownership. The membership scope is decided by the caller.
 */
static int
program_status_clear_client(struct client *c)
{
	if (!program_status_client_active(c))
		return (0);
	if (c->program_status == NULL || !c->program_status->cleanup)
		return (0);
	return (1);
}

/*
 * Emit a pane subtree clear. If output is blocked, the bytes are
 * discarded and the tty loss marks arm the owed reset, so no explicit
 * handling is needed here.
 */
static void
program_status_clear_pane(struct client *c, struct window_pane *wp)
{
	char	 buffer[80];

	xsnprintf(buffer, sizeof buffer,
	    "\033]7501;state=clear:id=tmux/p%u\033\\", wp->id);
	tty_puts(&c->tty, buffer);
}

/*
 * Validate an application-supplied ID: one to eight nonempty
 * slash-separated segments, each at most 32 bytes from the allowed set,
 * at most 128 bytes in total.
 */
static int
program_status_valid_id(const char *id)
{
	const char	*start = id;
	size_t		 size, segments = 0;

	if (*id == '\0' || strlen(id) > PROGRAM_STATUS_ID_LIMIT)
		return (0);
	for (;;) {
		size = strcspn(start, "/");
		if (size == 0 || size > PROGRAM_STATUS_SEGMENT_LIMIT)
			return (0);
		if (strspn(start, PROGRAM_STATUS_ID_CHARS) != size)
			return (0);
		segments++;
		if (segments > PROGRAM_STATUS_LEVELS_LIMIT)
			return (0);
		if (start[size] == '\0')
			break;
		start += size + 1;
	}
	return (1);
}

static void
program_status_probe_timeout(__unused int fd, __unused short events,
    void *data)
{
	struct program_status_client	*psc = data;

	/* No reply inside the query window: the terminal is unsupported. */
	psc->pending = 0;
}

/*
 * Issue the support probe. Any probe closes the report gate until its
 * reply confirms or it times out.
 */
static void
program_status_probe(struct client *c, struct program_status_client *psc)
{
	struct timeval		 tv = { .tv_sec = TTY_QUERY_TIMEOUT };

	psc->pending = 1;
	psc->supported = 0;
	tty_puts(&c->tty, PROGRAM_STATUS_PROBE);
	evtimer_add(&psc->timer, &tv);
}

/*
 * Discharge the owed namespace clear: emit it now, or arm the reset
 * obligation when delivery is not possible yet. raw selects the
 * bounded best-effort transport for paths after which no buffered
 * flush can occur: suspend is followed by tty_stop_tty (which
 * removes the write event) and detach by tty_close (which frees the
 * buffer unflushed), so only a direct write can deliver the clear
 * there. A buffered clear additionally requires a live write event:
 * a stopped tty (suspend or lock) has no event_out and nothing
 * re-arms it until the resume, so queued bytes could not flush before
 * the final close - the obligation stays armed for the resume sync or
 * the stop hook instead. A dead or stalled connection cannot
 * guarantee cleanup.
 */
static void
program_status_clear_or_arm(struct client *c,
    struct program_status_client *psc, int raw)
{
	if (~c->tty.flags & TTY_BLOCK) {
		if (raw) {
			tty_raw(&c->tty, PROGRAM_STATUS_CLEAR_NS);
			psc->reset = 0;
		} else if (c->tty.flags & TTY_STARTED) {
			tty_puts(&c->tty, PROGRAM_STATUS_CLEAR_NS);
			psc->reset = 0;
		} else
			psc->reset = 1;
	} else
		psc->reset = 1;
}

/*
 * State admission for the owned namespace clear at the lifecycle
 * hooks: ownership plus live enablement or an armed obligation. This
 * says nothing about delivery - whether the clear can be emitted now
 * is program_status_clear_or_arm's decision.
 */
static int
program_status_owed(const struct program_status_client *psc)
{
	return (psc->cleanup && (!psc->disabled || psc->reset));
}

/*
 * Drop support state and arm the re-probe: the next sync pass on a
 * capable client issues a fresh probe.
 */
static void
program_status_client_rearm(struct program_status_client *psc)
{
	evtimer_del(&psc->timer);
	psc->pending = 0;
	psc->supported = 0;
	psc->disabled = 1;
}

/*
 * Sync one client: probe state machine, disable clear and owed-reset
 * retry. Runs from the server client check loop, session changes and
 * option changes.
 */
void
program_status_client_sync(struct client *c)
{
	struct program_status_client	*psc = c->program_status;

	if (!program_status_client_active(c))
		return;
	if (c->session == NULL)
		return;

	if (!options_get_number(global_options, "forward-program-status")) {
		/* Never-enabled or never-supported clients owe nothing. */
		if (psc == NULL)
			return;
		if (!psc->disabled) {
			/*
			 * Enabled to disabled: stop new reports, then the
			 * ordered namespace clear (or the owed retry).
			 */
			program_status_client_rearm(psc);
			if (psc->cleanup)
				program_status_clear_or_arm(c, psc, 0);
		} else if (psc->cleanup && psc->reset) {
			/* While disabled, retry only the armed reset. */
			program_status_clear_or_arm(c, psc, 0);
		}
		return;
	}

	/* Blocked output: keep every obligation and try again later. */
	if (c->tty.flags & TTY_BLOCK)
		return;

	if (psc == NULL) {
		/* Allocate at the first sync pass that sees the option on. */
		psc = c->program_status = xcalloc(1, sizeof *psc);
		evtimer_set(&psc->timer, program_status_probe_timeout, psc);
		psc->disabled = 1;
	}

	/*
	 * The owed clear is gated on ownership, not on support or pending,
	 * so a blocked disable cleanup survives a re-enable's re-probe.
	 */
	if (psc->cleanup && psc->reset)
		program_status_clear_or_arm(c, psc, 0);
	if (psc->disabled) {
		psc->disabled = 0;
		program_status_probe(c, psc);
	}
}

/*
 * A client changed session (old is the previous session, NULL on first
 * attach) or detached (the new session is NULL). Called after the new
 * session is assigned; a same-session call (window navigation within
 * the session) preserves all records.
 */
void
program_status_client_switch(struct client *c, struct session *old)
{
	struct program_status_client	*psc = c->program_status;

	if (psc == NULL || old == NULL)
		return;
	if (old == c->session)
		return;

	if (program_status_owed(psc)) {
		/*
		 * Bounded best-effort clear of the old session's records.
		 * The disabled-but-armed case is the locked client, whose
		 * discharge was deferred at the lock. Detach delivers it
		 * raw: tty_close follows and would free the buffered bytes
		 * unflushed.
		 */
		program_status_clear_or_arm(c, psc, c->session == NULL);
	}
	if (c->session == NULL) {
		/* Detach: arm the re-probe for the next capable attach. */
		program_status_client_rearm(psc);
	}
}

/*
 * Suspend: bounded best-effort clear, then arm the re-probe. The state
 * survives tty_stop_tty; the resume check-loop sync re-probes and emits
 * any owed clear.
 */
void
program_status_client_suspend(struct client *c)
{
	struct program_status_client	*psc = c->program_status;

	if (psc == NULL)
		return;
	if (program_status_owed(psc))
		program_status_clear_or_arm(c, psc, 1);
	if (!psc->disabled)
		program_status_client_rearm(psc);
}

/*
 * Server lock: a screen takeover, not a terminal handover, so nothing
 * is emitted here and cleanup ownership is preserved. But the owned
 * records stay in the outer terminal with input stopped, so their
 * discharge is a deferred obligation, not a cancelled one: arm the
 * owed clear, which the next teardown hook or the unlock sync emits.
 * Input also stops, so a pending probe would time out unseen during
 * the lock and leave the client permanently unsupported: cancel it
 * and arm the re-probe, which the unlock resume path issues.
 */
void
program_status_client_lock(struct client *c)
{
	struct program_status_client	*psc = c->program_status;

	if (psc == NULL)
		return;
	if (!psc->disabled) {
		/*
		 * Only the lock's rearm keeps live owned records: a client
		 * already disabled has discharged its clear (or still owes
		 * the armed reset) and gains no new obligation here.
		 */
		program_status_client_rearm(psc);
		if (psc->cleanup)
			psc->reset = 1;
	}
}

/*
 * The client's session is being destroyed. server_destroy_session()
 * clears c->session before server_client_set_session(), so the switch
 * hook cannot see the old session: discharge its records here, while
 * the client is still attached (a stopped-tty client keeps the
 * obligation armed; see program_status_clear_or_arm). The later
 * set_session passes old == NULL and emits nothing, so there is no
 * double clear; the window-unlinked sink also skips these clients once
 * their session is cleared or rehomed.
 */
void
program_status_client_session_destroying(struct client *c)
{
	struct program_status_client	*psc = c->program_status;

	if (psc == NULL)
		return;
	if (program_status_owed(psc))
		program_status_clear_or_arm(c, psc, 0);
}

/*
 * The server is exiting and this client is being torn down. Discharge
 * the owned namespace clear before the shutdown membership invalidation,
 * while the connection is still healthy. Never-enabled or
 * never-confirmed clients own nothing; an owed clear armed while
 * disabled is still owed here. When buffered output is still pending,
 * a direct write could be overtaken by its later flush (a stale report
 * would then follow the final clear), so the existing reset obligation
 * is armed instead and program_status_client_stop() makes the final
 * bounded attempt at tty_close, where the buffer is freed without
 * another dispatch and nothing can follow it.
 */
void
program_status_client_server_exiting(struct client *c)
{
	struct program_status_client	*psc = c->program_status;

	if (psc == NULL)
		return;
	if (!program_status_owed(psc))
		return;
	if (EVBUFFER_LENGTH(c->tty.out) == 0)
		program_status_clear_or_arm(c, psc, 1);
	else
		psc->reset = 1;
}

/*
 * Known output loss: arm the owed namespace clear for when output
 * resumes. Only a client that owns the namespace can owe one.
 */
void
program_status_client_lost(struct tty *tty)
{
	struct client	*c = tty->client;

	if (c->program_status == NULL)
		return;
	if (c->program_status->cleanup)
		c->program_status->reset = 1;
}

/*
 * Final client teardown (tty_close): free the state and its event. The
 * suspend path never reaches this. An owed clear that survived to here
 * (blocked at detach or session destruction, or deferred at server
 * exit because buffered output was still pending) gets one last
 * bounded raw attempt: the client sends MSG_EXITING before restoring
 * its terminal, and tty_close frees the output buffer immediately after
 * without another event dispatch, so nothing queued can follow the
 * write and an earlier delivered clear left reset clear, preventing a
 * double emission. The blocked-output gate is dropped deliberately:
 * this is the last chance, and tty_raw itself bounds the retries; a
 * failed write just loses the clear, as for any dead connection.
 *
 * tty->client is NULL here for a control client: tty_init never runs
 * for one, so the embedded tty stays zeroed while MSG_EXITING still
 * reaches tty_close.
 */
void
program_status_client_stop(struct tty *tty)
{
	struct client			*c = tty->client;
	struct program_status_client	*psc;

	if (c == NULL)
		return;
	psc = c->program_status;
	if (psc == NULL)
		return;
	if (psc->cleanup && psc->reset) {
		tty_raw(&c->tty, PROGRAM_STATUS_CLEAR_NS);
		psc->reset = 0;
	}
	evtimer_del(&psc->timer);
	free(psc);
	c->program_status = NULL;
}

/*
 * Apply a matched outer reply body (first pair "?" after trim). The
 * reply is always consumed by the parser; only applying it is gated on a
 * pending probe.
 */
void
program_status_client_reply(struct tty *tty, const char *body)
{
	struct client			*c = tty->client;
	struct program_status_client	*psc;
	char				*copy, *next, *pair;

	psc = c->program_status;
	if (psc == NULL || !psc->pending)
		return;

	copy = next = xstrdup(body);
	pair = strsep(&next, ":");
	if (strcmp(program_status_trim(pair), "?") == 0) {
		/*
		 * Support only: unknown keys and malformed pairs are
		 * ignored, the states and kinds lists are not stored and
		 * report fields pass through unchanged. The confirmation
		 * clear covers any residue.
		 */
		psc->pending = 0;
		psc->supported = 1;
		psc->cleanup = 1;
		psc->reset = 1;
		evtimer_del(&psc->timer);
	}
	free(copy);
}

/*
 * Forward one parsed report body to every eligible attached client. The
 * BEL flag says the observed frame ended with BEL rather than ST;
 * input.c maps its private terminator enum to this boolean at the call
 * boundary. The forwarder runs independently of local acceptance in
 * input_program_status().
 */
void
program_status_forward(struct window_pane *wp, const char *body, int bel)
{
	char		*copy, *next, *pair, *base, *parsed;
	char		*key, *value, *id = NULL;
	char		*nb, *frame;
	char		 translated[160];
	const char	*terminator;
	struct client	*c;
	size_t		 size, flen;
	int		 invalid = 0;

	if (wp == NULL)
		return;

	/*
	 * Option off (the default): no forwarding work at all, before any
	 * parsing or allocation. Owed cleanup is not affected - the
	 * disabled/reset retry lives in program_status_client_sync.
	 */
	if (!options_get_number(global_options, "forward-program-status"))
		return;

	/*
	 * Frame bound before parsing: the shared parser buffer accepts far
	 * larger sequences, so the forwarder enforces its own bound.
	 */
	terminator = bel ? "\007" : "\033\\";
	if (strlen(body) + 7 + strlen(terminator) > PROGRAM_STATUS_FRAME_LIMIT)
		return;

	/*
	 * Validate all id fields first, with the local parser's pair
	 * conventions: any bare or invalid value drops the whole report;
	 * valid duplicates use the last value.
	 */
	copy = next = xstrdup(body);
	while ((pair = strsep(&next, ":")) != NULL) {
		parsed = pair;
		key = strsep(&parsed, "=");
		key = program_status_trim(key);
		if (strcmp(key, "id") == 0) {
			value = parsed == NULL ? NULL :
			    program_status_trim(parsed);
			if (value == NULL || !program_status_valid_id(value))
				invalid = 1;
			else {
				free(id);
				id = xstrdup(value);
			}
		}
		if (invalid)
			break;
	}
	free(copy);
	if (invalid) {
		free(id);
		return;
	}

	/*
	 * Translate the identity into the fixed namespace: root reports map
	 * to tmux/p<pane>, child reports to tmux/p<pane>/<id>.
	 */
	if (id == NULL)
		xsnprintf(translated, sizeof translated, "tmux/p%u", wp->id);
	else {
		xsnprintf(translated, sizeof translated, "tmux/p%u/%s",
		    wp->id, id);
		free(id);
	}

	/*
	 * Re-enforce the ID limits after translation: a pane-rooted child ID
	 * can exceed limits the original did not.
	 */
	if (!program_status_valid_id(translated))
		return;

	/*
	 * Rebuild the body with the translated id first, then every non-ID
	 * pair verbatim and in order, including unknown keys.
	 */
	size = strlen(body) + strlen(translated) + 16;
	nb = xmalloc(size);
	xsnprintf(nb, size, "id=%s", translated);
	copy = next = xstrdup(body);
	while ((pair = strsep(&next, ":")) != NULL) {
		base = xstrdup(pair);
		parsed = base;
		key = strsep(&parsed, "=");
		key = program_status_trim(key);
		if (strcmp(key, "id") != 0) {
			strlcat(nb, ":", size);
			strlcat(nb, pair, size);
		}
		free(base);
	}
	free(copy);

	/* Re-check the frame bound after rewriting. Never truncate. */
	flen = 7 + strlen(nb) + strlen(terminator);
	if (flen > PROGRAM_STATUS_FRAME_LIMIT) {
		free(nb);
		return;
	}

	frame = xmalloc(flen + 1);
	xsnprintf(frame, flen + 1, "\033]7501;%s%s", nb, terminator);
	free(nb);

	/*
	 * Emit to each eligible client, in this order: the owed namespace
	 * clear (skipped under TTY_BLOCK, where it would be lost), then the
	 * report. Reports before support confirmation are dropped, not
	 * retained, and a pane linked into a client's session more than
	 * once produces exactly one copy per client.
	 */
	TAILQ_FOREACH(c, &clients, entry) {
		if (!program_status_client_active(c))
			continue;
		if (c->session == NULL || !session_has(c->session, wp->window))
			continue;
		if (c->tty.flags & TTY_BLOCK)
			continue;
		if (c->program_status == NULL)
			continue;
		if (c->program_status->reset)
			program_status_clear_or_arm(c, c->program_status, 0);
		if (c->program_status->supported &&
		    !c->program_status->disabled)
			tty_puts(&c->tty, frame);
	}
	free(frame);
}

/*
 * The pane's forwarded records are void (it was destroyed or respawned,
 * and a respawned pane keeps its id): clear its subtree for each
 * eligible client of the pane's window.
 */
void
program_status_pane_cleared(struct window_pane *wp)
{
	struct client	*c;

	TAILQ_FOREACH(c, &clients, entry) {
		if (!program_status_clear_client(c))
			continue;
		if (c->session == NULL || !session_has(c->session, wp->window))
			continue;
		program_status_clear_pane(c, wp);
	}
}

/*
 * The pane-moved event fires after the reassignment, with both the old
 * and the new window alive, so this observes the same state the
 * window-unlinked sink does for departing windows.
 */
static void
program_status_pane_moved_cb(__unused const char *name,
    struct event_payload *ep, __unused void *data)
{
	struct window_pane	*wp = event_payload_get_pane(ep, "pane");
	struct window		*old_w = event_payload_get_window(ep, "old_window");
	struct client		*c;

	if (wp == NULL || old_w == NULL)
		return;

	/*
	 * Pane moved without destruction: clear only for clients that
	 * lost the pane (old window in their session, new window not).
	 * Still-member scopes keep their records; within-session moves
	 * clear nothing.
	 */
	TAILQ_FOREACH(c, &clients, entry) {
		if (!program_status_clear_client(c))
			continue;
		if (c->session == NULL)
			continue;
		if (!session_has(c->session, old_w) ||
		    session_has(c->session, wp->window))
			continue;
		program_status_clear_pane(c, wp);
	}
}

/*
 * A window is departing this session: its last link into the session is
 * going away, so clear each pane subtree for the session's eligible
 * clients. A window still linked into the session through another
 * winlink keeps its scope.
 */
void
program_status_window_departing(struct session *s, struct window *w)
{
	struct winlink		*wl;
	struct window_pane	*wp;
	struct client		*c;
	u_int			 links = 0;

	if (s == NULL || w == NULL)
		return;

	/*
	 * At the session.c fire sites the firing winlink is still in the
	 * tree, so more than one link means the window is still linked
	 * into this session; the group-synchronize site fires only when
	 * no replacement link exists. The cmd-swap-window.c caller runs
	 * before the reassignment, so the same count classifies the
	 * cross-session swap.
	 */
	RB_FOREACH(wl, winlinks, &s->windows) {
		if (wl->window == w)
			links++;
	}
	if (links > 1)
		return;

	TAILQ_FOREACH(wp, &w->panes, entry) {
		TAILQ_FOREACH(c, &clients, entry) {
			if (!program_status_clear_client(c))
				continue;
			if (c->session != s)
				continue;
			program_status_clear_pane(c, wp);
		}
	}
}

/*
 * The window-unlinked event fires at the semantic unlink sites, before
 * the winlink is removed, so the window and its panes are alive here.
 */
static void
program_status_window_unlinked_cb(__unused const char *name,
    struct event_payload *ep, __unused void *data)
{
	struct session	*s = event_payload_get_session(ep, "session");
	struct window	*w = event_payload_get_window(ep, "window");

	program_status_window_departing(s, w);
}

/*
 * Register the server-lifetime event sinks. Like the control client
 * sinks, they are never removed: the server does not free itself, so
 * the sink pointer is not stored.
 */
void
program_status_build_events(void)
{
	events_add_sink("window-unlinked",
	    program_status_window_unlinked_cb, NULL);
	events_add_sink("pane-moved",
	    program_status_pane_moved_cb, NULL);
}
