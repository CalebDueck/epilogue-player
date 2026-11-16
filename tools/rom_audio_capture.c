/* Render a ROM through mGBA and write its native audio to a stereo WAV.
 *
 * Usage: rom_audio_capture ROM SECONDS OUTPUT.wav [FRAME:KEYMASK,...]
 * Key masks use mGBA's bit positions: A=1, B=2, Select=4, Start=8,
 * Right=16, Left=32, Up=64, Down=128, R=256, L=512.
 *
 * This is a local extraction helper. It needs the mGBA development headers
 * and library; it does not bundle ROM or audio assets.
 */
#include <errno.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/core.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define OUTPUT_RATE 32768
#define FRAME_AUDIO_LIMIT 2048

struct InputEvent {
	long frame;
	uint32_t keys;
};

static FILE *output;
static uint64_t sample_count;
static short left_samples[FRAME_AUDIO_LIMIT];
static short right_samples[FRAME_AUDIO_LIMIT];

static void write_u16(FILE *file, uint16_t value) {
	fputc(value & 0xFF, file);
	fputc((value >> 8) & 0xFF, file);
}

static void write_u32(FILE *file, uint32_t value) {
	write_u16(file, value & 0xFFFF);
	write_u16(file, value >> 16);
}

static void write_wav_header(FILE *file, uint32_t data_bytes) {
	fwrite("RIFF", 1, 4, file);
	write_u32(file, 36 + data_bytes);
	fwrite("WAVEfmt ", 1, 8, file);
	write_u32(file, 16);
	write_u16(file, 1);
	write_u16(file, 2);
	write_u32(file, OUTPUT_RATE);
	write_u32(file, OUTPUT_RATE * 4);
	write_u16(file, 4);
	write_u16(file, 16);
	fwrite("data", 1, 4, file);
	write_u32(file, data_bytes);
}

static void write_interleaved(const short *left, const short *right, int count) {
	for (int i = 0; i < count; ++i) {
		write_u16(output, (uint16_t) left[i]);
		write_u16(output, (uint16_t) right[i]);
	}
	sample_count += (uint64_t) count;
}

static void post_gb_audio(struct mAVStream *stream, struct blip_t *left,
			  struct blip_t *right) {
	(void) stream;
	int count = blip_samples_avail(left);
	int right_count = blip_samples_avail(right);
	if (count > right_count) count = right_count;
	if (count > FRAME_AUDIO_LIMIT) count = FRAME_AUDIO_LIMIT;
	if (count <= 0) return;
	int got_left = blip_read_samples(left, left_samples, count, 0);
	int got_right = blip_read_samples(right, right_samples, count, 0);
	if (got_left < count) count = got_left;
	if (got_right < count) count = got_right;
	write_interleaved(left_samples, right_samples, count);
}

static int parse_events(const char *text, struct InputEvent *events, int capacity) {
	if (!text || !*text) return 0;
	char *copy = strdup(text);
	if (!copy) return -1;
	int count = 0;
	char *save = NULL;
	for (char *item = strtok_r(copy, ",", &save); item;
	     item = strtok_r(NULL, ",", &save)) {
		char *colon = strchr(item, ':');
		if (!colon || count >= capacity) {
			free(copy);
			return -1;
		}
		*colon = '\0';
		char *frame_end = NULL;
		char *keys_end = NULL;
		errno = 0;
		long frame = strtol(item, &frame_end, 10);
		unsigned long keys = strtoul(colon + 1, &keys_end, 0);
		if (errno || !frame_end || *frame_end || !keys_end || *keys_end ||
		    frame < 0 || keys > 0x3FF) {
			free(copy);
			return -1;
		}
		events[count++] = (struct InputEvent) {frame, (uint32_t) keys};
	}
	free(copy);
	return count;
}

int main(int argc, char **argv) {
	if (argc < 4 || argc > 5) {
		fprintf(stderr, "usage: %s ROM SECONDS OUTPUT.wav [FRAME:KEYMASK,...]\n", argv[0]);
		return 2;
	}
	char *seconds_end = NULL;
	long seconds = strtol(argv[2], &seconds_end, 10);
	if (!seconds_end || *seconds_end || seconds < 1 || seconds > 3600) {
		fprintf(stderr, "duration must be between 1 and 3600 seconds\n");
		return 2;
	}
	struct InputEvent events[256];
	int event_count = parse_events(argc == 5 ? argv[4] : NULL, events, 256);
	if (event_count < 0) {
		fprintf(stderr, "invalid input event list\n");
		return 2;
	}

	struct mCore *core = mCoreFind(argv[1]);
	if (!core || !core->init(core)) {
		fprintf(stderr, "mGBA could not initialize a core for %s\n", argv[1]);
		return 1;
	}
	mCoreInitConfig(core, "mgba-rom-audio-capture");
	core->opts.volume = 0x100;
	core->opts.mute = false;
	core->opts.skipBios = true;
	core->opts.useBios = false;
	mCoreConfigLoadDefaults(&core->config, &core->opts);
	mCoreLoadConfig(core);
	if (!mCoreLoadFile(core, argv[1])) {
		fprintf(stderr, "mGBA could not load %s\n", argv[1]);
		return 1;
	}

	unsigned width = 240, height = 160;
	core->desiredVideoDimensions(core, &width, &height);
	color_t *video = calloc((size_t) width * height, sizeof(*video));
	if (!video) return 1;
	core->setVideoBuffer(core, video, width);

	struct blip_t *left = core->getAudioChannel(core, 0);
	struct blip_t *right = core->getAudioChannel(core, 1);
	if (!left || !right) {
		fprintf(stderr, "mGBA did not provide stereo audio buffers\n");
		return 1;
	}
	blip_set_rates(left, core->frequency(core), OUTPUT_RATE);
	blip_set_rates(right, core->frequency(core), OUTPUT_RATE);

	struct mAVStream av = {0};
	if (core->platform(core) == mPLATFORM_GB) {
		av.postAudioBuffer = post_gb_audio;
		core->setAVStream(core, &av);
		core->setAudioBufferSize(core, 512);
	} else {
		int samples_per_frame = (int) ((double) OUTPUT_RATE * core->frameCycles(core) /
						       core->frequency(core) + 0.5);
		core->setAudioBufferSize(core, samples_per_frame * 2);
	}
	core->reset(core);

	output = fopen(argv[3], "wb+");
	if (!output) {
		perror(argv[3]);
		return 1;
	}
	write_wav_header(output, 0);
	long total_frames = seconds * 60;
	int next_event = 0;
	for (long frame = 0; frame < total_frames; ++frame) {
		while (next_event < event_count && events[next_event].frame == frame) {
			core->setKeys(core, events[next_event++].keys);
		}
		core->runFrame(core);
		if (core->platform(core) == mPLATFORM_GBA) {
			int count = blip_samples_avail(left);
			int right_count = blip_samples_avail(right);
			if (count > right_count) count = right_count;
			if (count > FRAME_AUDIO_LIMIT) count = FRAME_AUDIO_LIMIT;
			if (count > 0) {
				int got_left = blip_read_samples(left, left_samples, count, 0);
				int got_right = blip_read_samples(right, right_samples, count, 0);
				if (got_left < count) count = got_left;
				if (got_right < count) count = got_right;
				write_interleaved(left_samples, right_samples, count);
			}
		}
	}
	if (sample_count > UINT32_MAX / 4) {
		fprintf(stderr, "render exceeded the WAV size limit\n");
		return 1;
	}
	if (fseek(output, 0, SEEK_SET) != 0) return 1;
	write_wav_header(output, (uint32_t) sample_count * 4);
	fclose(output);
	fprintf(stderr, "rendered %.2f seconds of audio from %s\n",
		(double) sample_count / OUTPUT_RATE, argv[1]);
	return 0;
}
