#!/bin/sh

. ./input-common.inc

REPLY=$(mktemp)
trap 'rm -f "$TMP" "$EXP" "$REPLY"; $TMUX kill-server 2>/dev/null' 0 1 15

check_status()
{
	status_target=$1
	status_revision=$2
	status_records=$3
	status_id=$($TMUX display-message -p -t "$status_target" '#{pane_id}')
	printf '{"schema":1,"pane_id":"%s","revision":"%s","records":%s}\n' \
	    "$status_id" "$status_revision" "$status_records" >"$EXP"
	$TMUX display-message -p -t "$status_target" '#{pane_program_status}' >"$TMP"
	cmp -s "$TMP" "$EXP" || fail "$status_target snapshot"
	actual=$($TMUX display-message -p -t "$status_target" '#{pane_program_status_revision}')
	if [ "$actual" != "$status_revision" ]; then
		echo "FAIL: $status_target revision: $actual != $status_revision"
		exit_status=1
	fi
}

check_states()
{
	actual=$($TMUX display-message -p -t "$1" '#{pane_program_status_states}')
	if [ "$actual" != "$2" ]; then
		echo "FAIL: $1 states: $actual != $2"
		exit_status=1
	fi
}

report()
{
	start_pane "$1" 80 5 "\033]7501;$2\007"
	check_status "$1:" 1 "$3"
}

reject()
{
	start_pane "$1" 80 5 "\033]7501;state=idle\007\033]7501;$2\007"
	check_status "$1:" 1 '[{"id":"","state":"idle"}]'
}

wait_pending()
{
	i=0
	while [ "$i" -lt 100 ]; do
		$TMUX capture-pane -pPC -t "$1" >"$TMP"
		printf '%s\n' "$2" >"$EXP"
		cmp -s "$TMP" "$EXP" && return
		sleep 0.03
		i=$((i + 1))
	done
	fail "$1 pending input"
}

wait_format()
{
	i=0
	while [ "$i" -lt 100 ]; do
		actual=$($TMUX display-message -p -t "$1" "$2")
		[ "$actual" = "$3" ] && return
		sleep 0.03
		i=$((i + 1))
	done
	echo "FAIL: $1 $2: $actual != $3"
	exit_status=1
}

start_pane empty 80 5 ''
check_status empty: 0 '[]'
check_states empty: ''

report bel 'state=blocked:kind=permission:progress=042:app=clnkr:title=VGVzdA==:msg=SGVsbG8' \
    '[{"id":"","state":"blocked","kind":"permission","progress":42,"app":"clnkr","title":"Test","msg":"Hello","effective_app":"clnkr"}]'
check_states bel: blocked

start_pane st 80 5 '\033]7501;state=working\033\\'
check_status st: 1 '[{"id":"","state":"working"}]'

report text 'state=done:msg=4piDICJcIiAje3BhbmVfaWR9:title=' \
    '[{"id":"","state":"done","title":"","msg":"☃ \"\\\" #{pane_id}"}]'

report whitespace ' state = working : app = clnkr : bad : =empty : x=y : state=blocked : kind=question ' \
    '[{"id":"","state":"blocked","kind":"question","app":"clnkr","effective_app":"clnkr"}]'
report controls 'state=done:app=cln\000kr:state=wo\011rking:msg=SGVsbG8=:title=SGV\177sbG8=' \
    '[{"id":"","state":"done","msg":"Hello"}]'
report ignored-fields 'state=idle:kind=auth:progress=9:app=bad/name' \
    '[{"id":"","state":"idle"}]'
report unknown-kind 'state=blocked:kind=later:progress=101' \
    '[{"id":"","state":"blocked"}]'
report invalid-progress 'state=working:progress=+1' '[{"id":"","state":"working"}]'
report duplicate-id 'state=done:id=first:id=second' '[{"id":"second","state":"done"}]'

for body in 'app=clnkr' 'state=future' 'state=working:state=future' \
    'state=working:id=' 'state=working:id=/x' 'state=working:id=x/' \
    'state=working:id=x//y' 'state=working:id=bad,name' \
    'state=working:id=bad!id' 'state=working:id=bad id' \
    'state=working:id=bad\000id' \
    'state=working:msg=_' 'state=working:msg=A' 'state=working:msg=TQ=' \
    'state=working:msg=AA==' 'state=working:msg=Cg==' \
    'state=working:title=CQ==' 'state=working:msg=fw==' \
    'state=working:msg=woA=' 'state=working:msg=wp8=' \
    'state=working:msg=wK8=' 'state=working:msg=7aCA' \
    'state=working:msg=9JCAgA==' 'state=working:msg=4pg=' \
    'state=working:msg=AA==:msg=b2s=' \
    'state=clear:title=AA==:title=b2s='; do
	reject invalid "$body"
done

for id in 'bad!id' 'bad id' 'bad\000id'; do
	start_pane invalid-clear 80 5 "\033]7501;state=done:app=original\007\033]7501;state=done:id=child\007\033]7501;state=clear:id=$id\007"
	check_status invalid-clear: 2 '[{"id":"","state":"done","app":"original","effective_app":"original"},{"id":"child","state":"done","effective_app":"original"}]'
done

start_pane replace 80 5 '\033]7501;state=blocked:kind=auth:app=first:msg=eA==\007\033]7501;state=working\007\033]7501;state=working:kind=ignored\007'
check_status replace: 2 '[{"id":"","state":"working"}]'

start_cmd hierarchy 80 5 "printf '\033]7501;state=idle:app=root\007\033]7501;state=done:id=z/a/b\007\033]7501;state=working:id=z:app=parent\007\033]7501;state=blocked:id=a:app=own\007'; $TMUX wait-for duplicate; printf '\033]7501;state=working:id=z:app=parent:kind=ignored\007\033]7501;'; $TMUX wait-for changed; printf 'state=error:id=z:app=next\007'; $INPUT_HOLD"
wait_format hierarchy: '#{pane_program_status_revision}' 4
records='[{"id":"","state":"idle","app":"root","effective_app":"root"},{"id":"a","state":"blocked","app":"own","effective_app":"own"},{"id":"z","state":"working","app":"parent","effective_app":"parent"},{"id":"z/a/b","state":"done","effective_app":"parent"}]'
check_status hierarchy: 4 "$records"
check_states hierarchy: 'blocked done idle working'
$TMUX wait-for -S duplicate
wait_pending hierarchy: '\033]7501;'
check_status hierarchy: 4 "$records"
check_states hierarchy: 'blocked done idle working'
$TMUX wait-for -S changed
wait_format hierarchy: '#{pane_program_status_revision}' 5
check_status hierarchy: 5 '[{"id":"","state":"idle","app":"root","effective_app":"root"},{"id":"a","state":"blocked","app":"own","effective_app":"own"},{"id":"z","state":"error","app":"next","effective_app":"next"},{"id":"z/a/b","state":"done","effective_app":"next"}]'
check_states hierarchy: 'blocked done error idle'

start_pane clear 80 5 '\033]7501;state=idle:app=root\007\033]7501;state=done:id=a/b\007\033]7501;state=done:id=ab\007\033]7501;state=clear:id=a\007\033]7501;state=clear:id=missing\007'
check_status clear: 4 '[{"id":"","state":"idle","app":"root","effective_app":"root"},{"id":"ab","state":"done","effective_app":"root"}]'
start_pane clear-root 80 5 '\033]7501;state=done:id=a/b\007\033]7501;state=clear\007\033]7501;state=clear\007'
check_status clear-root: 2 '[]'

longapp=$(printf '%032d' 0)
report app-limit "state=done:app=$longapp" \
    "[{\"id\":\"\",\"state\":\"done\",\"app\":\"$longapp\",\"effective_app\":\"$longapp\"}]"
reject app-over "state=working:app=${longapp}x:app=ok"
report key-limit 'state=working:abcdefghijklmnop=ignored' '[{"id":"","state":"working"}]'
reject key-over 'state=working:abcdefghijklmnopq=ignored'
report depth-limit 'state=done:id=a/b/c/d/e/f/g/h' '[{"id":"a/b/c/d/e/f/g/h","state":"done"}]'
reject depth-over 'state=working:id=a/b/c/d/e/f/g/h/i:id=ok'
longid="$longapp/$longapp/$longapp/$(printf '%029d' 0)"
report id-limit "state=done:id=$longid" "[{\"id\":\"$longid\",\"state\":\"done\"}]"
reject id-over "state=working:id=${longid}x:id=ok"
reject segment-over "state=working:id=${longapp}x:id=ok"

for field in msg title; do
	if [ "$field" = msg ]; then limit=2048; else limit=192; fi
	decoded=$(printf "%${limit}s" '' | tr ' ' a)
	encoded=$(printf '%s' "$decoded" | base64 | tr -d '\n')
	report text-limit "state=done:$field=$encoded" \
	    "[{\"id\":\"\",\"state\":\"done\",\"$field\":\"$decoded\"}]"
	encoded=$(printf '%sx' "$decoded" | base64 | tr -d '\n')
	reject text-over "state=working:$field=$encoded:$field=b2s="
done
reject encoded-over "state=working:msg=$(printf '%02733d' 0):msg=b2s="

# The framing and ignored C0 bytes count toward the raw sequence limit.
for end in '\007' '\033\\'; do
	if [ "$end" = '\007' ]; then framing=8; else framing=9; fi
	for total in 4096 4097; do
		padding=$((total - framing - 16))
		start_pane sequence-limit 80 5 "\033]7501;state=working:x=$(printf "%0${padding}d" 0)$end"
		if [ "$total" = 4096 ]; then
			check_status sequence-limit: 1 '[{"id":"","state":"working"}]'
		else
			check_status sequence-limit: 0 '[]'
		fi
		padding=$((padding - 1))
		start_pane raw-limit 80 5 "\033]7501;state=working:x=$(printf "%0${padding}d" 0)\000$end"
		if [ "$total" = 4096 ]; then
			check_status raw-limit: 1 '[{"id":"","state":"working"}]'
		else
			check_status raw-limit: 0 '[]'
		fi
	done
done

for sequence in '\033]7501;state=done\030' '\033]7501;state=done\032' \
    '\033]7501;state=done\033x' '\033]7501;state=done\033\000\\' \
    '\033]7501;state=done\033\033\\' \
    '\033]7501;state=done\033\000\033\\' \
    '\033]7501;state=done\033\177\\' \
    '\033]7501;state=done\033 \\' \
    '\033]07501;state=done\007' '\033]75\00001;state=done\007' \
    '\033\000]7501;state=done\007'; do
	start_pane aborted 80 5 "$sequence\033]7501;state=working\007"
	check_status aborted: 1 '[{"id":"","state":"working"}]'
done
start_pane interrupted 80 5 '\033]7501;state=done\033]7501;state=working\007'
check_status interrupted: 1 '[{"id":"","state":"working"}]'

# Each pending capture proves the previous fragment was parsed before resuming.
start_cmd fragmented 80 5 "printf '\033]75'; $TMUX wait-for selector; printf '01;state=done'; $TMUX wait-for body; printf '\033'; $TMUX wait-for st; printf '\\\\'; $INPUT_HOLD"
wait_pending fragmented: '\033]75'
check_status fragmented: 0 '[]'
$TMUX wait-for -S selector
wait_pending fragmented: '\033]7501;state=done'
check_status fragmented: 0 '[]'
$TMUX wait-for -S body
wait_pending fragmented: '\033]7501;state=done\033'
check_status fragmented: 0 '[]'
$TMUX wait-for -S st
wait_format fragmented: '#{pane_program_status_revision}' 1
check_status fragmented: 1 '[{"id":"","state":"done"}]'

start_cmd reset-pending 80 5 "printf '\033]7501;state=done'; $TMUX wait-for reset; printf '\033\\\\\033]7501;state=working\007'; $INPUT_HOLD"
wait_pending reset-pending: '\033]7501;state=done'
$TMUX send-keys -R -t reset-pending:
$TMUX wait-for -S reset
wait_format reset-pending: '#{pane_program_status_revision}' 1
check_status reset-pending: 1 '[{"id":"","state":"working"}]'

start_pane screens 80 5 '\033]7501;state=working\007\033[?1049h\033[!p\033[?1049l'
check_status screens: 1 '[{"id":"","state":"working"}]'
start_pane ris 80 5 '\033]7501;state=done\007\033c\033]7501;state=done\007\033c'
check_status ris: 4 '[]'

lifetime='\033]7501;state=working:app=root\007\033]7501;state=done:id=a\007\033]7501;state=blocked:id=b\007\033]7501;state=error:id=b/child\007\033]7501;state=idle:id=c\007'
survivors='[{"id":"a","state":"done"},{"id":"b/child","state":"error"},{"id":"c","state":"idle"}]'
start_pane prompt 80 5 "$lifetime\033]133;A\007"
check_status prompt: 6 "$survivors"
check_states prompt: 'done error idle'
start_pane other-prompt 80 5 '\033]7501;state=working\007\033]133;N\007'
check_status other-prompt: 1 '[{"id":"","state":"working"}]'
start_cmd process-exit 80 5 "$TMUX wait-for finish; printf '$lifetime'"
$TMUX set-option -p -t process-exit: remain-on-exit on
$TMUX wait-for -S finish
wait_format process-exit: '#{pane_dead}' 1
check_status process-exit: 6 "$survivors"
$TMUX respawn-pane -t process-exit: "$INPUT_HOLD"
wait_format process-exit: '#{pane_dead}' 0
check_status process-exit: 7 '[]'
$TMUX respawn-pane -k -t process-exit: "printf '\033]7501;state=working\007'; $INPUT_HOLD"
wait_format process-exit: '#{pane_program_status_revision}' 8
check_status process-exit: 8 '[{"id":"","state":"working"}]'

start_pane identity 80 5 '\033]7501;state=done\007'
first=$($TMUX display-message -p -t identity: '#{pane_id}')
second=$($TMUX split-window -dP -F '#{pane_id}' -t identity: "$INPUT_HOLD")
check_status "$second" 0 '[]'
$TMUX swap-pane -s "$first" -t "$second"
check_status "$first" 1 '[{"id":"","state":"done"}]'
$TMUX new-session -d -s destination "$INPUT_HOLD"
$TMUX join-pane -d -s "$first" -t destination:
check_status "$first" 1 '[{"id":"","state":"done"}]'
check_status "$second" 0 '[]'

start_cmd eviction 80 5 "i=0; while [ \"\$i\" -lt 256 ]; do printf '\033]7501;state=done:id=%03d\007' \"\$i\"; i=\$((i + 1)); done; printf '\033]7501;state=done:id=000\007\033]7501;state=done:id=256\007'; $INPUT_HOLD"
records='['
i=0
while [ "$i" -le 256 ]; do
	if [ "$i" -ne 1 ]; then
		[ "$i" -ne 0 ] && records="$records,"
		records="$records$(printf '{"id":"%03d","state":"done"}' "$i")"
	fi
	i=$((i + 1))
done
check_status eviction: 257 "$records]"

start_cmd eviction-parent 80 5 "printf '\033]7501;state=idle:app=root\007\033]7501;state=done:id=child\007'; i=0; while [ \"\$i\" -lt 255 ]; do printf '\033]7501;state=done:id=%03d\007' \"\$i\"; i=\$((i + 1)); done; $INPUT_HOLD"
records='['
i=0
while [ "$i" -lt 255 ]; do
	[ "$i" -ne 0 ] && records="$records,"
	records="$records$(printf '{"id":"%03d","state":"done"}' "$i")"
	i=$((i + 1))
done
check_status eviction-parent: 257 "$records,{\"id\":\"child\",\"state\":\"done\"}]"

reply='\033]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\033\\'
reply_length=$(printf "$reply$reply" | wc -c)
start_cmd query 80 5 "stty raw -echo; printf '\033]7501;?\007\033]7501;?\033\\'; dd bs=1 count=$reply_length 2>/dev/null >'$REPLY'; $INPUT_HOLD"
printf "$reply$reply" >"$EXP"
cp "$REPLY" "$TMP"
cmp -s "$TMP" "$EXP" || fail 'fixed feature replies'
check_status query: 0 '[]'

start_cmd query-abort 80 5 "stty raw -echo; printf '\033]7501;?\033x\033]7501;?:id=secret\007\033[5n'; dd bs=1 count=4 2>/dev/null >'$REPLY'; $INPUT_HOLD"
printf '\033[0n' >"$EXP"
cp "$REPLY" "$TMP"
cmp -s "$TMP" "$EXP" || fail 'aborted query and untrusted fields'

# Existing OSC handlers still ignore C0 and dispatch titles as before.
start_pane legacy-title 80 5 '\033]2;leg\000acy\007'
wait_format legacy-title: '#{pane_title}' legacy
start_cmd legacy-st 80 5 "printf '\033]2;fragmented\033'; $TMUX wait-for legacy; printf '\\\\'; $INPUT_HOLD"
wait_format legacy-st: '#{pane_title}' fragmented
$TMUX wait-for -S legacy
wait_format legacy-st: '#{pane_title}' fragmented

$TMUX kill-server 2>/dev/null
exit $exit_status
