// SPDX-License-Identifier: GPL-3
/*
 * HP OMEN RGB Keyboard Driver - Zone Management
 *
 * RGB zone management and LED control
 *
 * Author: alessandromrc
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/device.h>
#include <linux/leds.h>
#include <linux/string.h>

#include "omen_rgb_keyboard.h"
#include "omen_wmi.h"
#include "omen_zones.h"
#include "omen_animations.h"
#include "omen_state.h"
#include "omen_hda_led.h"

struct device_attribute *zone_dev_attrs;
struct attribute **zone_attrs;
struct platform_zone *zone_data;

static struct platform_device *zone_platform_dev;

struct platform_zone original_colors[ZONE_COUNT];
int global_brightness = 100;

struct led_classdev omen_kbd_led;

static struct attribute_group zone_attribute_group = {
	.name = "rgb_zones",
};

int parse_rgb(const char *buf, struct platform_zone *zone)
{
	unsigned long rgb;
	int ret;
	union color_union {
		struct color_platform cp;
		int package;
	} repackager;

	ret = kstrtoul(buf, 16, &rgb);
	if (ret)
		return ret;
	if (rgb > 0xFFFFFF)
		return -EINVAL;

	repackager.package = rgb;
	pr_debug("r:%d g:%d b:%d\n",
		 repackager.cp.red, repackager.cp.green, repackager.cp.blue);
	zone->colors = repackager.cp;
	return 0;
}

struct platform_zone *match_zone(struct device_attribute *attr)
{
	u8 zone;
	for (zone = 0; zone < ZONE_COUNT; zone++) {
		if ((struct device_attribute *)zone_data[zone].attr == attr)
			return &zone_data[zone];
	}
	return NULL;
}

int fourzone_update_led(struct platform_zone *zone, enum hp_wmi_command rw)
{
	u8 state[128];
	int ret = hp_wmi_perform_query(HPWMI_FOURZONE_COLOR_GET, HPWMI_FOURZONE,
				       &state, sizeof(state), sizeof(state));
	if (ret) {
		pr_warn("fourzone_color_get returned error 0x%x\n", ret);
		return ret <= 0 ? ret : -EINVAL;
	}

	if (rw == HPWMI_WRITE) {
		state[zone->offset + 0] = zone->colors.red;
		state[zone->offset + 1] = zone->colors.green;
		state[zone->offset + 2] = zone->colors.blue;

		ret = hp_wmi_perform_query(HPWMI_FOURZONE_COLOR_SET, HPWMI_FOURZONE,
					   &state, sizeof(state), sizeof(state));
		if (ret)
			pr_warn("fourzone_color_set returned error 0x%x\n", ret);
		return ret;
	} else {
		zone->colors.red = state[zone->offset + 0];
		zone->colors.green = state[zone->offset + 1];
		zone->colors.blue = state[zone->offset + 2];
	}
	return 0;
}

/*
 * Write every zone with a single WMI round trip.
 *
 * fourzone_update_led() re-reads the whole 128-byte state before each write,
 * so painting four zones one at a time costs four get/set pairs. Animations do
 * exactly that on every frame, which is where most of the per-frame time went.
 */
int fourzone_update_all_leds(struct color_platform colors[ZONE_COUNT])
{
	u8 state[128];
	int ret = hp_wmi_perform_query(HPWMI_FOURZONE_COLOR_GET, HPWMI_FOURZONE,
				       &state, sizeof(state), sizeof(state));
	if (ret) {
		pr_warn("fourzone_color_get returned error 0x%x\n", ret);
		return ret <= 0 ? ret : -EINVAL;
	}

	for (u8 zone = 0; zone < ZONE_COUNT; zone++) {
		state[zone_data[zone].offset + 0] = colors[zone].red;
		state[zone_data[zone].offset + 1] = colors[zone].green;
		state[zone_data[zone].offset + 2] = colors[zone].blue;
	}

	ret = hp_wmi_perform_query(HPWMI_FOURZONE_COLOR_SET, HPWMI_FOURZONE,
				   &state, sizeof(state), sizeof(state));
	if (ret)
		pr_warn("fourzone_color_set returned error 0x%x\n", ret);
	return ret;
}

void apply_brightness_to_color(struct color_platform *color)
{
	color->red = (color->red * global_brightness) / 100;
	color->green = (color->green * global_brightness) / 100;
	color->blue = (color->blue * global_brightness) / 100;
}

void update_all_zones_with_colors(struct color_platform colors[ZONE_COUNT])
{
	struct color_platform scaled[ZONE_COUNT];

	for (int zone = 0; zone < ZONE_COUNT; zone++) {
		zone_data[zone].colors = colors[zone];
		apply_brightness_to_color(&zone_data[zone].colors);
		scaled[zone] = zone_data[zone].colors;
	}

	fourzone_update_all_leds(scaled);
}

static int omen_apply_brightness(unsigned long level)
{
	int ret;

	if (level > 100)
		level = 100;

	global_brightness = level;

	for (int zone = 0; zone < ZONE_COUNT; zone++) {
		zone_data[zone].colors.red = (original_colors[zone].colors.red * level) / 100;
		zone_data[zone].colors.green = (original_colors[zone].colors.green * level) / 100;
		zone_data[zone].colors.blue = (original_colors[zone].colors.blue * level) / 100;

		ret = fourzone_update_led(&zone_data[zone], HPWMI_WRITE);
		if (ret)
			return ret;
	}

	save_animation_state();
	return 0;
}

enum led_brightness omen_kbd_brightness_get(struct led_classdev *led_cdev)
{
	return global_brightness;
}

int omen_kbd_brightness_set(struct led_classdev *led_cdev,
			    enum led_brightness value)
{
	return omen_apply_brightness(value);
}

ssize_t zone_show(struct device *dev, struct device_attribute *attr,
		  char *buf)
{
	struct platform_zone *target_zone = match_zone(attr);
	int ret;
	if (target_zone == NULL)
		return sprintf(buf, "red: -1, green: -1, blue: -1\n");
	ret = fourzone_update_led(target_zone, HPWMI_READ);
	if (ret)
		return sprintf(buf, "red: -1, green: -1, blue: -1\n");
	return sprintf(buf, "#%02x%02x%02x\n",
		       target_zone->colors.red,
		       target_zone->colors.green, target_zone->colors.blue);
}

ssize_t zone_set(struct device *dev, struct device_attribute *attr,
		 const char *buf, size_t count)
{
	struct platform_zone *target_zone = match_zone(attr);
	int ret;
	if (target_zone == NULL) {
		pr_err("invalid target zone\n");
		return -EINVAL;
	}
	ret = parse_rgb(buf, target_zone);
	if (ret)
		return ret;

	int zone_idx = target_zone - zone_data;
	original_colors[zone_idx].colors.red = target_zone->colors.red;
	original_colors[zone_idx].colors.green = target_zone->colors.green;
	original_colors[zone_idx].colors.blue = target_zone->colors.blue;

	animation_stop();
	animation_set_mode(ANIMATION_STATIC);

	target_zone->colors.red = (target_zone->colors.red * global_brightness) / 100;
	target_zone->colors.green = (target_zone->colors.green * global_brightness) / 100;
	target_zone->colors.blue = (target_zone->colors.blue * global_brightness) / 100;

	ret = fourzone_update_led(target_zone, HPWMI_WRITE);
	if (ret)
		return ret;
	
	/* Save state */
	save_animation_state();
	
	return count;
}

ssize_t brightness_show(struct device *dev, struct device_attribute *attr,
			char *buf)
{
	return sprintf(buf, "%d\n", global_brightness);
}

ssize_t brightness_set(struct device *dev, struct device_attribute *attr,
		       const char *buf, size_t count)
{
	unsigned long level;
	int ret;

	if (kstrtoul(buf, 10, &level))
		return -EINVAL;

	ret = omen_apply_brightness(level);
	if (ret)
		return ret;

	return count;
}

ssize_t all_show(struct device *dev, struct device_attribute *attr,
		 char *buf)
{
	int ret;
	ret = fourzone_update_led(&zone_data[0], HPWMI_READ);
	if (ret)
		return sprintf(buf, "red: -1, green: -1, blue: -1\n");
	return sprintf(buf, "#%02x%02x%02x\n",
		       zone_data[0].colors.red,
		       zone_data[0].colors.green, zone_data[0].colors.blue);
}

ssize_t all_set(struct device *dev, struct device_attribute *attr,
		const char *buf, size_t count)
{
	struct platform_zone temp;
	int ret;
	u8 z;

	ret = parse_rgb(buf, &temp);
	if (ret)
		return ret;

	animation_stop();
	animation_set_mode(ANIMATION_STATIC);

	for (z = 0; z < ZONE_COUNT; z++) {
		/* Store the new color as the original color */
		original_colors[z].colors = temp.colors;

		zone_data[z].colors.red = (temp.colors.red * global_brightness) / 100;
		zone_data[z].colors.green = (temp.colors.green * global_brightness) / 100;
		zone_data[z].colors.blue = (temp.colors.blue * global_brightness) / 100;

		ret = fourzone_update_led(&zone_data[z], HPWMI_WRITE);
		if (ret)
			return ret;
	}

	/* Save state */
	save_animation_state();

	return count;
}

/*
 * "zones" -- set all four zones at once, e.g.
 *     echo "FF0000 00FF00 0000FF FFFFFF" > zones
 *
 * Same effect as four writes to zone00..zone03, but one WMI round trip
 * instead of four, which is what makes smooth userspace animation possible.
 */
static ssize_t zones_show(struct device *dev, struct device_attribute *attr,
			  char *buf)
{
	u8 state[128];
	int len = 0;
	int ret = hp_wmi_perform_query(HPWMI_FOURZONE_COLOR_GET, HPWMI_FOURZONE,
				       &state, sizeof(state), sizeof(state));
	if (ret)
		return sysfs_emit(buf, "\n");

	for (u8 zone = 0; zone < ZONE_COUNT; zone++)
		len += sysfs_emit_at(buf, len, "%s#%02x%02x%02x",
				     zone ? " " : "",
				     state[zone_data[zone].offset + 0],
				     state[zone_data[zone].offset + 1],
				     state[zone_data[zone].offset + 2]);

	return len + sysfs_emit_at(buf, len, "\n");
}

static ssize_t zones_set(struct device *dev, struct device_attribute *attr,
			 const char *buf, size_t count)
{
	struct color_platform colors[ZONE_COUNT];
	struct platform_zone parsed;
	const char *p = buf;
	int ret;

	for (u8 zone = 0; zone < ZONE_COUNT; zone++) {
		char token[16];
		int n = 0;

		while (*p == ' ' || *p == '\t' || *p == '\n')
			p++;
		while (*p && *p != ' ' && *p != '\t' && *p != '\n' &&
		       n < (int)sizeof(token) - 1)
			token[n++] = *p++;
		token[n] = '\0';

		if (!n)
			return -EINVAL;
		ret = parse_rgb(token, &parsed);
		if (ret)
			return ret;
		colors[zone] = parsed.colors;
	}

	animation_stop();
	animation_set_mode(ANIMATION_STATIC);

	for (u8 zone = 0; zone < ZONE_COUNT; zone++) {
		original_colors[zone].colors = colors[zone];
		zone_data[zone].colors = colors[zone];
		apply_brightness_to_color(&zone_data[zone].colors);
		colors[zone] = zone_data[zone].colors;
	}

	ret = fourzone_update_all_leds(colors);
	if (ret)
		return ret;

	save_animation_state();
	return count;
}

static DEVICE_ATTR(zones, 0664, zones_show, zones_set);

static ssize_t mute_led_show(struct device *dev, struct device_attribute *attr,
			     char *buf)
{
	/* We don't have a way to read the LED state, so just return help text */
	return sprintf(buf, "Write '1' to turn on, '0' to turn off\n");
}

static ssize_t mute_led_set(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	unsigned long value;
	int ret;

	ret = kstrtoul(buf, 10, &value);
	if (ret)
		return ret;

	/* Set mute button LED state based on value */
	ret = omen_hda_led_set(value ? true : false);
	if (ret)
		return ret;

	return count;
}

static DEVICE_ATTR(mute_led, 0664, mute_led_show, mute_led_set);

static ssize_t mute_state_show(struct device *dev, struct device_attribute *attr,
			       char *buf)
{
	return sprintf(buf, "Write '1' for muted, '0' for unmuted (from userspace daemon)\n");
}

static ssize_t mute_state_set(struct device *dev, struct device_attribute *attr,
			      const char *buf, size_t count)
{
	unsigned long value;
	int ret;

	ret = kstrtoul(buf, 10, &value);
	if (ret)
		return ret;

	/* Set mute state from userspace (e.g., PipeWire daemon) */
	ret = omen_hda_led_set_mute_state(value ? true : false);
	if (ret)
		return ret;

	return count;
}

static DEVICE_ATTR(mute_state, 0664, mute_state_show, mute_state_set);

int fourzone_setup(struct platform_device *dev)
{
	u8 zone;
	char buffer[10];
	char *name;
	int ret;

	zone_dev_attrs = kcalloc(ZONE_COUNT + 6, sizeof(struct device_attribute),
				 GFP_KERNEL);
	if (!zone_dev_attrs)
		return -ENOMEM;

	zone_attrs = kcalloc(ZONE_COUNT + 9, sizeof(struct attribute *),
			     GFP_KERNEL);
	if (!zone_attrs) {
		ret = -ENOMEM;
		goto err_free_dev_attrs;
	}

	zone_data = kcalloc(ZONE_COUNT, sizeof(struct platform_zone),
			    GFP_KERNEL);
	if (!zone_data) {
		ret = -ENOMEM;
		goto err_free_zone_attrs;
	}

	for (u8 zone = 0; zone < ZONE_COUNT; zone++) {
		zone_data[zone].offset = 25 + (zone * 3);
		ret = fourzone_update_led(&zone_data[zone], HPWMI_READ);
		if (ret)
			goto err_free_zone_data;

		/* Store original colors */
		original_colors[zone].colors.red = zone_data[zone].colors.red;
		original_colors[zone].colors.green = zone_data[zone].colors.green;
		original_colors[zone].colors.blue = zone_data[zone].colors.blue;
	}

	for (zone = 0; zone < ZONE_COUNT; zone++) {
		sprintf(buffer, "zone%02hhX", zone);
		name = kstrdup(buffer, GFP_KERNEL);
		if (!name) {
			ret = -ENOMEM;
			goto err_free_names;
		}

		sysfs_attr_init(&zone_dev_attrs[zone].attr);
		zone_dev_attrs[zone].attr.name = name;
		zone_dev_attrs[zone].attr.mode = 0664;
		zone_dev_attrs[zone].show = zone_show;
		zone_dev_attrs[zone].store = zone_set;
		zone_data[zone].offset = 25 + (zone * 3);
		zone_attrs[zone] = &zone_dev_attrs[zone].attr;
		zone_data[zone].attr = &zone_dev_attrs[zone];
	}

	sysfs_attr_init(&zone_dev_attrs[ZONE_COUNT].attr);
	zone_dev_attrs[ZONE_COUNT].attr.name = "all";
	zone_dev_attrs[ZONE_COUNT].attr.mode = 0664;
	zone_dev_attrs[ZONE_COUNT].show = all_show;
	zone_dev_attrs[ZONE_COUNT].store = all_set;
	zone_attrs[ZONE_COUNT] = &zone_dev_attrs[ZONE_COUNT].attr;

	zone_attrs[ZONE_COUNT + 1] = &animation_brightness_attr.attr;
	zone_attrs[ZONE_COUNT + 2] = &animation_mode_attr.attr;
	zone_attrs[ZONE_COUNT + 3] = &animation_speed_attr.attr;
	zone_attrs[ZONE_COUNT + 4] = &gradient_config_attr.attr;
	zone_attrs[ZONE_COUNT + 5] = &dev_attr_mute_led.attr;
	zone_attrs[ZONE_COUNT + 6] = &dev_attr_mute_state.attr;
	zone_attrs[ZONE_COUNT + 7] = &dev_attr_zones.attr;
	zone_attrs[ZONE_COUNT + 8] = NULL; /* NULL terminate the array */

	zone_attribute_group.attrs = zone_attrs;

	ret = sysfs_create_group(&dev->dev.kobj, &zone_attribute_group);
	if (ret)
		goto err_free_names;

	zone_platform_dev = dev;
	omen_kbd_led.name = "omen::kbd_backlight";
	omen_kbd_led.max_brightness = 100;
	omen_kbd_led.brightness_set_blocking = omen_kbd_brightness_set;
	omen_kbd_led.brightness_get = omen_kbd_brightness_get;
	return 0;

err_free_names:
	for (u8 z = 0; z < ZONE_COUNT; z++) {
		if (zone_dev_attrs[z].attr.name)
			kfree(zone_dev_attrs[z].attr.name);
	}
err_free_zone_data:
	kfree(zone_data);
	zone_data = NULL;
err_free_zone_attrs:
	kfree(zone_attrs);
	zone_attrs = NULL;
err_free_dev_attrs:
	kfree(zone_dev_attrs);
	zone_dev_attrs = NULL;
	return ret;
}

void fourzone_cleanup(void)
{
	/* Remove sysfs group before freeing backing memory */
	if (zone_platform_dev)
		sysfs_remove_group(&zone_platform_dev->dev.kobj, &zone_attribute_group);

	/* Free allocated zone attribute names */
	if (zone_dev_attrs) {
		for (u8 zone = 0; zone < ZONE_COUNT; zone++) {
			if (zone_dev_attrs[zone].attr.name)
				kfree(zone_dev_attrs[zone].attr.name);
		}
	}

	kfree(zone_dev_attrs);
	kfree(zone_attrs);
	kfree(zone_data);
}

