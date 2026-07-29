const MOCK_CASES = [
  {
    id: "S1",
    label: "Why is Crude Charge Pump P-101 vibrating?",
    rootCause:
      "Operating point shift after a +12% flow setpoint increase likely amplified sensitivity after recent seal replacement.",
    confidence: 0.93,
    recommendation:
      "Rollback setpoint to prior range and schedule seal alignment recheck on EQ-1042 at first safe opportunity.",
    entity: "FAC1-U100-P101",
    timeline: [
      {
        time: "2026-07-21 06:00",
        source: "CMMS",
        text: "WO-4471 opened for EQ-1042 mechanical seal replacement."
      },
      {
        time: "2026-07-22 06:00",
        source: "CMMS",
        text: "WO-4471 closed, note flags alignment recheck due to shim shortfall."
      },
      {
        time: "2026-07-27 22:00",
        source: "DCS",
        text: "U100_P101 SP changed from 320.0 to 358.4 m3/h (+12%)."
      },
      {
        time: "2026-07-28 00:00",
        source: "APM",
        text: "Pump_001 predicted failure: bearing_wear (0.72 confidence)."
      },
      {
        time: "2026-07-28 02:40",
        source: "AM",
        text: "PMP-100-101.VIB HH alarm active at 11.4 mm/s."
      }
    ],
    evidence: [
      {
        title: "DCS Operator Action",
        source: "Data/dcs/dcs_operator_actions.csv",
        record:
          "DCS-A-0450, 2026-07-27T22:00:00Z, U100_P101, SP_CHANGE, 320.0 -> 358.4"
      },
      {
        title: "CMMS Work Order",
        source: "Data/cmms/cmms_workorders.csv",
        record: "WO-4471, EQ-1042, Closed, Replaced mechanical seal"
      },
      {
        title: "Alarm Event",
        source: "Data/am/am_alarm_events.csv",
        record: "AME-000002, PMP-100-101.VIB, HH, ACTIVE, 11.4"
      },
      {
        title: "ERP Posting",
        source: "Data/erp/erp_cost_postings.csv",
        record: "POST-9001, ERP-FAC1-PMP-100-101, linked_wo=WO-4471"
      }
    ],
    relationships: [
      { type: "canonical", label: "FAC1-U100-P101" },
      { type: "alias", label: "AM:PMP-100-101" },
      { type: "alias", label: "APM:Pump_001" },
      { type: "alias", label: "DCS:U100_P101" },
      { type: "alias", label: "CMMS:EQ-1042" },
      { type: "alias", label: "ERP:ERP-FAC1-PMP-100-101" }
    ]
  },
  {
    id: "S4",
    label: "Is K-101 the same problem as P-101?",
    rootCause:
      "No. K-101 vibration is linked to cooling valve CV-400 sticking and lube oil overtemperature, not pump flow setpoint behavior.",
    confidence: 0.89,
    recommendation:
      "Treat as a separate incident. Prioritize CV-400 corrective maintenance and verify lube cooling restoration.",
    entity: "FAC1-U100-K101",
    timeline: [
      {
        time: "2026-07-25 12:00",
        source: "CMMS",
        text: "WO-4530 opened on EQ-8001: CV-400 sticking, not tracking setpoint."
      },
      {
        time: "2026-07-25 08:00",
        source: "DCS",
        text: "U400_V101 SP change from 140.0 to 155.0 m3/h."
      },
      {
        time: "2026-07-26 14:00",
        source: "APM",
        text: "Compressor_005 anomaly detected: vib_trend_rising."
      },
      {
        time: "2026-07-26 15:00",
        source: "AM",
        text: "C-101.VIB HH alarm active."
      }
    ],
    evidence: [
      {
        title: "CMMS Valve Record",
        source: "Data/cmms/cmms_workorders.csv",
        record: "WO-4530, EQ-8001, Cooling water valve CV-400 reported sticking"
      },
      {
        title: "APM Event",
        source: "Data/apm/apm_events.csv",
        record: "APM-E-0008, Compressor_005, AnomalyDetected"
      },
      {
        title: "Historian Context",
        source: "Data/hist/historian_config.json",
        record: "FAC1.UNIT100.GAS_COMPRESSOR_101.LUBE_01 and VIB_02 monitored"
      }
    ],
    relationships: [
      { type: "canonical", label: "FAC1-U100-K101" },
      { type: "alias", label: "AM:C-101" },
      { type: "alias", label: "APM:Compressor_005" },
      { type: "alias", label: "DCS:U100_K101" },
      { type: "alias", label: "CMMS:EQ-6001" },
      { type: "linked", label: "CV-400 -> EQ-8001" }
    ]
  },
  {
    id: "S5",
    label: "Why are there so many alarms on TK-201?",
    rootCause:
      "Alarm flood is likely due to high-limit configuration near normal operating level, not an actual process upset.",
    confidence: 0.87,
    recommendation:
      "Re-rationalize high alarm threshold and deadband for TNK-200-101 in AM configuration.",
    entity: "FAC1-U200-TK201",
    timeline: [
      {
        time: "2026-07-27 06:00-08:39",
        source: "AM",
        text: "Repeated H/RTN oscillation events around 78.3-78.6% for TNK-200-101.LVL."
      },
      {
        time: "Current baseline",
        source: "HIST",
        text: "Storage tank level remains near stable normal band."
      }
    ],
    evidence: [
      {
        title: "AM Config",
        source: "Data/am/am_config.json",
        record: "TNK-200-101 high limit configured at 78.5%"
      },
      {
        title: "Alarm Event Burst",
        source: "Data/am/am_alarm_events.csv",
        record: "Multiple ACTIVE/RTN cycles for TNK-200-101.LVL"
      },
      {
        title: "Historian Tag",
        source: "Data/hist/historian_config.json",
        record: "FAC1.UNIT200.STORAGE_TANK_101.LVL_01"
      }
    ],
    relationships: [
      { type: "canonical", label: "FAC1-U200-TK201" },
      { type: "alias", label: "AM:TNK-200-101" },
      { type: "alias", label: "HIST:FAC1.UNIT200.STORAGE_TANK_101.*" }
    ]
  }
];
