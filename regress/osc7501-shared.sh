#!/bin/sh

. ./input-common.inc
FORMAT='#{pane_program_state}|#{pane_program_name}|#{pane_program_message}|#{pane_pb_state}|#{pane_pb_progress}'

check()
{
	start_pane shared 80 5 "$1"
	actual=$($TMUX display-message -p -t shared: "$FORMAT") || exit 1
	if [ "$actual" != "$2" ]; then
		echo "FAIL: expected <$2>, got <$actual>"
		exit_status=1
	fi
}

check '\033]7501;state=blocked:app=tool\007\033]9;4;1;42\007' 'blocked|tool||paused|42'
check '\033]9;4;1;42\007\033]7501;state=clear\007' '|||hidden|0'
check '\033]9;4;1;42\007\033]7501;state=working\007\033]133;A\007\033]9;4;4\007' 'blocked|||paused|0'

for seed in '' '\033]9;4;1;42\007'; do
	if [ -z "$seed" ]; then retained=0; else retained=42; fi
	for row in '0 hidden' '1 normal' '2 error' '3 indeterminate' '4 paused'; do
		set -- $row
		state=$1
		projection=$2
		case $state in
		0) semantic= ;;
		1|3) semantic=working ;;
		2) semantic=error ;;
		4) semantic=blocked ;;
		esac
		for value in absent empty 0 42 100; do
			case $value in
			absent) suffix=; percent=$retained ;;
			empty) suffix=';'; percent=$retained ;;
			*) suffix=";$value"; percent=$value ;;
			esac
			[ "$state" != 3 ] || percent=$retained
			check "$seed\033]9;4;$state$suffix\007" "$semantic|||$projection|$percent"
		done
	done
done

for rich in idle working done blocked error; do
	for row in '0 hidden' '1 normal' '2 normal' '3 indeterminate' '4 normal'; do
		set -- $row
		state=$1
		mode=$2
		for suffix in '' ';42'; do
			percent=0
			[ -z "$suffix" ] || [ "$state" = 3 ] || percent=42
			case $rich in
			idle|done) projection=hidden ;;
			blocked) projection=paused ;;
			error) projection=error ;;
			working) projection=$mode ;;
			esac
			check "\033]7501;state=$rich:app=tool:msg=b2s=\007\033]9;4;$state$suffix\007" "$rich|tool|ok|$projection|$percent"
			if [ "$rich" = working ] && [ "$state" = 0 ]; then projection=indeterminate; fi
			check "\033]9;4;$state$suffix\007\033]7501;state=$rich:app=tool:msg=b2s=\007" "$rich|tool|ok|$projection|$percent"
		done
	done
done

mixed='\033]7501;state=blocked:app=tool:msg=b2s=\007\033]9;4;1;42\007'
for state in 0 1 2 3 4; do
	for bad in -1 101 12x 42';x'; do
		check "$mixed\033]9;4;$state;$bad\007" 'blocked|tool|ok|paused|42'
	done
done
for report in 'state=future' 'state=clear:id=x' 'state=clear:msg=_' 'state=working:msg=AA==' '?'; do
	check "$mixed\033]7501;$report\007" 'blocked|tool|ok|paused|42'
done
check "$mixed\033]7501;state=blocked:progress=99\007" 'blocked|||paused|42'
check "$mixed\033]7501;state=done\007\033]7501;state=working\007" 'working|||normal|42'
check "$mixed\033]7501;state=idle\007\033]7501;state=working\007" 'working|||normal|42'
check '\033]7501;state=working\007' 'working|||indeterminate|0'
check '\033]7501;state=blocked\007' 'blocked|||paused|0'
check '\033]7501;state=working\007\033]9;4;0\007' 'working|||hidden|0'
check '\033]7501;state=working\007\033]9;4;0\007\033]7501;state=working\007' 'working|||indeterminate|0'
check "$mixed\033]7501;state=clear\007\033]9;4;2\007" 'error|||error|0'
for reset in '\033]133;A\007' '\033c'; do
	check "$mixed$reset" '|||hidden|0'
	check "\033]9;4;2;42\007$reset" 'error|||error|42'
	check "\033]9;4;2;42\007\033]7501;state=error\007$reset" '|||hidden|0'
	check "$mixed$reset\033]9;4;2\007" 'error|||error|0'
done
for preserve in '\033]133;N\007' '\033[?1049h' '\033[?1049h\033[?1049l'; do
	check "$mixed$preserve" 'blocked|tool|ok|paused|42'
done
check "$mixed" 'blocked|tool|ok|paused|42'
$TMUX copy-mode -t shared: || exit 1
actual=$($TMUX display-message -p -t shared: "$FORMAT") || exit 1
[ "$actual" = 'blocked|tool|ok|paused|42' ] || exit 1
$TMUX respawn-pane -k -t shared: "printf '\033]2;respawned\007'; $INPUT_HOLD" || exit 1
wait_for_output respawned $TMUX display-message -p -t shared: '#{pane_title}' || exit 1
actual=$($TMUX display-message -p -t shared: "$FORMAT") || exit 1
[ "$actual" = '|||hidden|0' ] || exit 1

check '\033]9;4;1;42\007\033]9;4;2\007\033]9;4;4\007\033]9;4;3\007\033]9;4;0\007\033]9;4;1\007' 'working|||normal|42'
[ "$exit_status" -ne 0 ] || echo 'PASS: shared status matrix'
exit "$exit_status"
