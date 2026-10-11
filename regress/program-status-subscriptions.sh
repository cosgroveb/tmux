#!/bin/sh

PATH=/bin:/usr/bin
TERM=screen
LC_ALL=C.UTF-8
LANG=C.UTF-8
export TERM LC_ALL LANG

[ -z "$TEST_TMUX" ] && TEST_TMUX=$(readlink -f ../tmux)
TMUX="$TEST_TMUX -LtestA$$ -f/dev/null"
TMPDIR=$(mktemp -d)
IN="$TMPDIR/in"
OUT="$TMPDIR/out"
PID=

cleanup()
{
	[ -n "$PID" ] && kill "$PID" 2>/dev/null
	$TMUX kill-server 2>/dev/null
	rm -rf "$TMPDIR"
}
trap cleanup EXIT

wait_for()
{
	pattern=$1
	i=0
	while [ "$i" -lt 60 ]; do
		grep -F -- "$pattern" "$OUT" >/dev/null && return 0
		sleep 0.1
		i=$((i + 1))
	done
	echo "missing: $pattern"
	cat "$OUT"
	exit 1
}

reject_since()
{
	pattern=$1
	i=0
	while [ "$i" -lt 30 ]; do
		if tail -n +"$after" "$OUT" | grep -F -- "$pattern"; then
			echo "unexpected: $pattern"
			exit 1
		fi
		sleep 0.1
		i=$((i + 1))
	done
}

send()
{
	printf '%s\n' "$*" >&3
}

attach()
{
	: >"$OUT"
	$TMUX -C attach-session -t subs <"$IN" >"$OUT" 2>&1 &
	PID=$!
	exec 3>"$IN"
	send 'display-message -p ready'
	wait_for ready
}

emit()
{
	$TMUX send-keys -t "$1" -l "printf '\033]7501;$2\007'" || exit 1
	$TMUX send-keys -t "$1" Enter || exit 1
}

wait_status()
{
	i=0
	while [ "$i" -lt 60 ]; do
		actual=$($TMUX display-message -p -t "$1" \
		    '#{pane_program_status_revision}:#{pane_program_status_states}') || exit 1
		[ "$actual" = "$2" ] && return 0
		sleep 0.1
		i=$((i + 1))
	done
	echo "pane $1: expected $2, got $actual"
	exit 1
}

snapshot()
{
	$TMUX display-message -p -t "$1" '#{pane_program_status}'
}

prefix()
{
	fields=$($TMUX display-message -p -t "$2" \
	    '#{window_id} #{window_index} #{pane_id}') || exit 1
	printf '%%subscription-changed %s %s %s : ' "$1" "$sid" "$fields"
}

$TMUX new-session -d -s subs -x 80 -y 24 /bin/sh || exit 1
sid=$($TMUX display-message -p -t subs '#{session_id}')
pane=$($TMUX display-message -p -t subs '#{pane_id}')
$TMUX new-session -d -s other /bin/sh || exit 1
mkfifo "$IN"
attach

# Initial subscriptions carry complete snapshots, including empty revision zero.
empty="{\"schema\":1,\"pane_id\":\"$pane\",\"revision\":\"0\",\"records\":[]}"
send "refresh-client -B 'all:%*:#{pane_program_status}'"
send "refresh-client -B 'one:$pane:#{pane_program_status}'"
all=$(prefix all "$pane")
one=$(prefix one "$pane")
wait_for "$all$empty"
wait_for "$one$empty"

emit "$pane" 'state=working:app=clnkr'
wait_status "$pane" '1:working'
wait_for "$all$(snapshot "$pane")"
wait_for "$one$(snapshot "$pane")"
emit "$pane" 'state=clear'
wait_status "$pane" '2:'
empty="{\"schema\":1,\"pane_id\":\"$pane\",\"revision\":\"2\",\"records\":[]}"
wait_for "$all$empty"
wait_for "$one$empty"

emit "$pane" 'state=done:msg=c2FtZQ=='
wait_status "$pane" '3:done'
wait_for "$all$(snapshot "$pane")"
# One write can complete a cycle between samples. Only the final state is promised.
emit "$pane" 'state=working\007\033]7501;state=done:msg=c2FtZQ=='
wait_status "$pane" '5:done'
final=$(snapshot "$pane")
wait_for "$all$final"
wait_for "$one$final"

send detach-client
exec 3>&-
wait "$PID" || exit 1
PID=
[ "$(snapshot "$pane")" = "$final" ] || exit 1
emit "$pane" 'state=done:msg=b2ZmbGluZQ=='
wait_status "$pane" '6:done'
final=$(snapshot "$pane")
attach
send "refresh-client -B 'all:%*:#{pane_program_status}'"
send "refresh-client -B 'one:$pane:#{pane_program_status}'"
wait_for "$all$final"
wait_for "$one$final"

# A linked window is reported for each index in the attached session.
$TMUX link-window -s subs:0 -t subs:5 || exit 1
linked_all=$(prefix all subs:5)
linked_one=$(prefix one subs:5)
wait_for "$linked_all$final"
wait_for "$linked_one$final"
$TMUX link-window -s subs:0 -t other:5 || exit 1
emit "$pane" 'state=blocked:kind=question'
wait_status "$pane" '7:blocked'
final=$(snapshot "$pane")
wait_for "$all$final"
wait_for "$linked_all$final"
wait_for "$one$final"
wait_for "$linked_one$final"

# Moving a pane outside the attached session preserves its records, but neither
# selector emits an update or a deletion snapshot for that pane afterwards.
moved=$($TMUX new-window -d -P -F '#{pane_id}' -t subs: -n moving /bin/sh) || exit 1
send "refresh-client -B 'moved:$moved:#{pane_program_status}'"
emit "$moved" 'state=working'
wait_status "$moved" '1:working'
moved_all=$(prefix all "$moved")
moved_one=$(prefix moved "$moved")
wait_for "$moved_all$(snapshot "$moved")"
wait_for "$moved_one$(snapshot "$moved")"
after=$(($(wc -l <"$OUT") + 1))
$TMUX join-pane -d -s "$moved" -t other:0 || exit 1
emit "$moved" 'state=done'
wait_status "$moved" '2:done'
reject_since " $moved : "
$TMUX list-panes -a -F '#{pane_program_status}' |
    grep -Fx -- "$(snapshot "$moved")" >/dev/null || exit 1

deleted=$($TMUX new-window -d -P -F '#{pane_id}' -t subs: -n deleted /bin/sh) || exit 1
send "refresh-client -B 'deleted:$deleted:#{pane_program_status}'"
emit "$deleted" 'state=done'
wait_status "$deleted" '1:done'
deleted_all=$(prefix all "$deleted")
deleted_one=$(prefix deleted "$deleted")
wait_for "$deleted_all$(snapshot "$deleted")"
wait_for "$deleted_one$(snapshot "$deleted")"
after=$(($(wc -l <"$OUT") + 1))
$TMUX kill-pane -t "$deleted" || exit 1
reject_since " $deleted : "
if $TMUX list-panes -a -F '#{pane_id}' | grep -Fx -- "$deleted"; then
	exit 1
fi

# All-pane subscriptions include retained dead panes; explicit ids skip them.
dead=$($TMUX new-window -d -P -F '#{pane_id}' -t subs: -n dead /bin/sh) || exit 1
$TMUX set-option -w -t "$dead" remain-on-exit on || exit 1
emit "$dead" 'state=done'
wait_status "$dead" '1:done'
$TMUX send-keys -t "$dead" -l exit || exit 1
$TMUX send-keys -t "$dead" Enter || exit 1
i=0
while [ "$($TMUX display-message -p -t "$dead" '#{pane_dead}')" != 1 ]; do
	[ "$i" -lt 60 ] || exit 1
	sleep 0.1
	i=$((i + 1))
done
dead_final=$(snapshot "$dead")
send "refresh-client -B 'dead-all:%*:#{pane_program_status}'"
send "refresh-client -B 'dead-one:$dead:#{pane_program_status}'"
wait_for "$(prefix dead-all "$dead")$dead_final"
after=1
reject_since '%subscription-changed dead-one '

exit 0
