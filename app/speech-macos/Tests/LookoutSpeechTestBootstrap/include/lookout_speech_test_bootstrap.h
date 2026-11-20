/*
 * Test-only: installs the trap engine for the whole test process when the test bundle loads.
 * Defined in Swift (Tests/LookoutSpeechMocks, @_cdecl); called by bootstrap.c's constructor.
 * In no product: it never reaches liblookout_speech.a or lookout-transcribe.
 */
#ifndef LOOKOUT_SPEECH_TEST_BOOTSTRAP_H
#define LOOKOUT_SPEECH_TEST_BOOTSTRAP_H

void lk_speech_tests_install_trap(void);

#endif
