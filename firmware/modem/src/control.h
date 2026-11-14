// Line-based control protocol on the CDC port.
//
// Each command is one line; the reply is zero or more "key=value" or text
// lines, then "OK" or "ERR <reason>". Unsolicited events are lines starting
// with "! " (e.g. "! status on", "! ri", "! pcm on"). Type "help" for the list.
#pragma once

void control_init(void);
void control_task(void);
void control_event(const char *event);
