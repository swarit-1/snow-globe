/*
 * Test-only: installs the trap engine for the whole test process when the test bundle loads.
 * Defined in Swift (Tests/SnowGlobeSpeechMocks, @_cdecl); called by bootstrap.c's constructor.
 * In no product: it never reaches libsnow_globe_speech.a or snow-globe-transcribe.
 */
#ifndef SNOW_GLOBE_SPEECH_TEST_BOOTSTRAP_H
#define SNOW_GLOBE_SPEECH_TEST_BOOTSTRAP_H

void sg_speech_tests_install_trap(void);

#endif
