// SPDX-License-Identifier: GPL-3
/*
 * HP OMEN RGB Keyboard Driver - HDA LED Control
 *
 * HDA codec interface for controlling the mute button LED
 *
 * Author: alessandromrc
 */

#ifndef OMEN_HDA_LED_H
#define OMEN_HDA_LED_H

/**
 * omen_hda_led_set - Set the mute button LED state
 * @on: true to turn LED on, false to turn it off
 *
 * Returns: 0 on success, negative error code on failure
 */
int omen_hda_led_set(bool on);

/**
 * omen_hda_led_init - Initialize HDA LED control
 *
 * Returns: 0 on success, negative error code on failure
 */
int omen_hda_led_init(void);

/**
 * omen_hda_led_cleanup - Cleanup HDA LED control
 */
void omen_hda_led_cleanup(void);

/**
 * omen_hda_led_set_mute_state - Set mute state from userspace (e.g., PipeWire daemon)
 * @muted: true if muted, false if unmuted
 *
 * This allows a userspace daemon to notify the kernel driver about mute state
 * changes from PipeWire/Bluetooth devices that aren't accessible via ALSA.
 *
 * Returns: 0 on success, negative error code on failure
 */
int omen_hda_led_set_mute_state(bool muted);

#endif /* OMEN_HDA_LED_H */

