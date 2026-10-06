"""Regressions for physical waveform direction and overshoot recovery."""
import struct
import unittest
from ezpd_protocol import SyncPDSample, SyncScopeSample, analyze_avs_transitions


def analyze(target,base,moving,near,plateau):
    payload=struct.pack('<II',(11<<28)|(int(round(target/0.025))<<9)|100,0xd3c096f0)
    pd=[SyncPDSample(0,1,'EPR_REQUEST',100_000,100_500,None,payload),
        SyncPDSample(1,2,'ACCEPT',105_000,105_500,None,b''),
        SyncPDSample(2,3,'PS_RDY',150_000,150_500,None,b'')]
    scope=[SyncScopeSample(t,base if t<=100_000 else moving if t==120_000
        else near if t==140_000 else plateau)for t in range(0,540_001,20_000)]
    return analyze_avs_transitions(pd,scope,movement_sustain_samples=2,
        settle_max_sample_gap_us=50_000,plateau_min_samples=6)[0]


class TransitionTests(unittest.TestCase):
    def test_down_despite_nominal_target_above_baseline(self):
        a=analyze(47,46.5,46.2,45.6,45.5)
        self.assertEqual(a.direction,'down');self.assertEqual(a.movement_start_us,120_000)
        self.assertIsNone(a.target_crossing_us)
        self.assertIn('target_already_beyond_at_request',a.flags)
        self.assertLess(a.observed_average_slew_V_per_s,0)

    def test_up_despite_nominal_target_below_baseline(self):
        a=analyze(17,17.5,17.8,18.4,18.5)
        self.assertEqual(a.direction,'up');self.assertEqual(a.movement_start_us,120_000)
        self.assertIsNone(a.target_crossing_us)
        self.assertIn('target_already_beyond_at_request',a.flags)

    def test_overshoot_recovery_not_ramp_slew(self):
        a=analyze(16,15,16.5,16,16)
        self.assertEqual(a.direction,'up');self.assertIsNone(a.average_slew_V_per_s)
        self.assertIsNone(a.observed_average_slew_V_per_s)
        self.assertIn('observed_slew_opposes_movement',a.flags)
        self.assertIn('absolute_slew_opposes_movement',a.flags)
        self.assertIsNotNone(a.observed_settling_us)
