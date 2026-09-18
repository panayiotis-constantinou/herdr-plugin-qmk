use midir::{MidiOutput, MidiOutputConnection};
use serde::Deserialize;
use std::{
    collections::{HashMap, HashSet},
    env,
    error::Error,
    io::{BufRead, BufReader, ErrorKind, Write},
    os::unix::net::UnixStream,
    path::Path,
    thread,
    time::Duration,
};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const MIDI_CHANNEL: u8 = 0xBF;
const CC_HEARTBEAT: u8 = 110;
const CC_STATE: u8 = 111;
const CC_SLOT_FIRST: u8 = 112;
const PROTOCOL: u8 = 1;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
enum Status {
    Idle,
    Working,
    Blocked,
    Done,
    Unknown,
}

impl Status {
    fn code(self) -> u8 {
        match self {
            Self::Idle => 0,
            Self::Working => 1,
            Self::Blocked => 2,
            Self::Done => 3,
            Self::Unknown => 4,
        }
    }
}

#[derive(Debug, Deserialize)]
struct Agent {
    pane_id: String,
    agent_status: Status,
    #[serde(default)]
    state_change_seq: u64,
}

#[derive(Deserialize)]
struct Snapshot {
    agents: Vec<Agent>,
}

#[derive(Deserialize)]
struct SnapshotResult {
    snapshot: Snapshot,
}

#[derive(Deserialize)]
struct Response {
    result: SnapshotResult,
}

#[derive(Debug, Eq, PartialEq)]
struct Frame {
    aggregate: Status,
    slots: [u8; 4],
    any_working: bool,
    overflow: bool,
    chime_done: bool,
    chime_blocked: bool,
}

struct Tracker {
    slots: [Option<String>; 4],
    previous: HashMap<String, Status>,
}

impl Tracker {
    fn new() -> Self {
        Self {
            slots: [None, None, None, None],
            previous: HashMap::new(),
        }
    }

    fn update(&mut self, mut agents: Vec<Agent>, notify: bool) -> Frame {
        agents.sort_by(|a, b| {
            a.state_change_seq
                .cmp(&b.state_change_seq)
                .then_with(|| a.pane_id.cmp(&b.pane_id))
        });

        let live: HashSet<&str> = agents.iter().map(|a| a.pane_id.as_str()).collect();
        for slot in &mut self.slots {
            if slot.as_deref().is_some_and(|id| !live.contains(id)) {
                *slot = None;
            }
        }
        for agent in &agents {
            if self
                .slots
                .iter()
                .any(|slot| slot.as_deref() == Some(&agent.pane_id))
            {
                continue;
            }
            if let Some(slot) = self.slots.iter_mut().find(|slot| slot.is_none()) {
                *slot = Some(agent.pane_id.clone());
            }
        }

        let current: HashMap<String, Status> = agents
            .iter()
            .map(|agent| (agent.pane_id.clone(), agent.agent_status))
            .collect();
        let changed_to = |status| {
            notify
                && current.iter().any(|(id, now)| {
                    *now == status
                        && self
                            .previous
                            .get(id)
                            .is_some_and(|before| *before != status)
                })
        };

        let any = |status| agents.iter().any(|agent| agent.agent_status == status);
        let aggregate = if any(Status::Blocked) {
            Status::Blocked
        } else if any(Status::Working) {
            Status::Working
        } else if any(Status::Done) {
            Status::Done
        } else if any(Status::Unknown) {
            Status::Unknown
        } else {
            Status::Idle
        };
        let slots = std::array::from_fn(|index| {
            self.slots[index]
                .as_ref()
                .and_then(|id| current.get(id))
                .map_or(7, |status| status.code())
        });
        let frame = Frame {
            aggregate,
            slots,
            any_working: any(Status::Working),
            overflow: agents.len() > self.slots.len(),
            chime_done: changed_to(Status::Done),
            chime_blocked: changed_to(Status::Blocked),
        };
        self.previous = current;
        frame
    }
}

fn snapshot(path: &Path) -> Result<Vec<Agent>> {
    let mut stream = UnixStream::connect(path)?;
    stream.write_all(
        b"{\"id\":\"qmk-herdr-snapshot\",\"method\":\"session.snapshot\",\"params\":{}}\n",
    )?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line)?;
    Ok(serde_json::from_str::<Response>(&line)?
        .result
        .snapshot
        .agents)
}

fn heartbeat(midi: &mut MidiOutputConnection) -> Result<()> {
    midi.send(&[MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL])?;
    Ok(())
}

fn send_frame(midi: &mut MidiOutputConnection, frame: &Frame) -> Result<()> {
    heartbeat(midi)?;
    for (index, status) in frame.slots.iter().enumerate() {
        midi.send(&[MIDI_CHANNEL, CC_SLOT_FIRST + index as u8, *status])?;
    }
    let mut value = frame.aggregate.code();
    value |= u8::from(frame.any_working) << 3;
    value |= u8::from(frame.overflow) << 4;
    value |= u8::from(frame.chime_done) << 5;
    value |= u8::from(frame.chime_blocked) << 6;
    midi.send(&[MIDI_CHANNEL, CC_STATE, value])?;
    Ok(())
}

fn pane_ids(agents: &[Agent]) -> HashSet<String> {
    agents.iter().map(|agent| agent.pane_id.clone()).collect()
}

fn subscription_request(agents: &[Agent]) -> serde_json::Value {
    let mut subscriptions = vec![
        serde_json::json!({"type": "pane.agent_detected"}),
        serde_json::json!({"type": "pane.closed"}),
        serde_json::json!({"type": "pane.exited"}),
    ];
    subscriptions.extend(agents.iter().map(
        |agent| serde_json::json!({"type": "pane.agent_status_changed", "pane_id": agent.pane_id}),
    ));
    serde_json::json!({
        "id": "qmk-herdr-subscribe",
        "method": "events.subscribe",
        "params": {"subscriptions": subscriptions}
    })
}

fn watch_session(
    path: &Path,
    midi: &mut MidiOutputConnection,
    tracker: &mut Tracker,
) -> Result<()> {
    let initial = snapshot(path)?;
    let subscribed = pane_ids(&initial);
    let mut stream = UnixStream::connect(path)?;
    stream.set_read_timeout(Some(Duration::from_secs(1)))?;
    writeln!(stream, "{}", subscription_request(&initial))?;

    let mut reader = BufReader::new(stream.try_clone()?);
    let mut line = String::new();
    reader.read_line(&mut line)?;
    let ack: serde_json::Value = serde_json::from_str(&line)?;
    if ack.pointer("/result/type").and_then(|value| value.as_str()) != Some("subscription_started")
    {
        return Err(format!("Herdr rejected event subscription: {line}").into());
    }

    let agents = snapshot(path)?;
    let agent_set_changed = pane_ids(&agents) != subscribed;
    send_frame(midi, &tracker.update(agents, false))?;
    if agent_set_changed {
        return Err("Herdr agent set changed; resubscribing".into());
    }
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => return Err("Herdr event stream closed".into()),
            Ok(_) => {
                let agents = snapshot(path)?;
                let agent_set_changed = pane_ids(&agents) != subscribed;
                send_frame(midi, &tracker.update(agents, true))?;
                if agent_set_changed {
                    return Err("Herdr agent set changed; resubscribing".into());
                }
            }
            Err(error) if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                heartbeat(midi)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
}

fn open_midi(name: &str) -> Result<MidiOutputConnection> {
    let midi = MidiOutput::new("qmk-herdr")?;
    let needle = name.to_lowercase();
    let ports = midi.ports();
    let mut matches = Vec::new();
    let mut available = Vec::new();
    for port in &ports {
        let port_name = midi.port_name(port)?;
        if port_name.to_lowercase().contains(&needle) {
            matches.push(port.clone());
        }
        available.push(port_name);
    }
    if matches.len() != 1 {
        return Err(format!(
            "expected one MIDI output matching {name:?}, found {}; available: {}",
            matches.len(),
            available.join(", ")
        )
        .into());
    }
    Ok(midi.connect(&matches[0], "qmk-herdr")?)
}

fn run() -> Result<()> {
    let socket = env::var_os("HERDR_SOCKET_PATH")
        .ok_or("HERDR_SOCKET_PATH is missing; run qmk-herdr inside a Herdr pane")?;
    let port = env::args().nth(1).unwrap_or_else(|| "Planck EZ".into());
    let mut tracker = Tracker::new();

    loop {
        let attempt = (|| -> Result<()> {
            let mut midi = open_midi(&port)?;
            eprintln!("qmk-herdr: connected to MIDI output matching {port:?}");
            watch_session(Path::new(&socket), &mut midi, &mut tracker)
        })();
        if let Err(error) = attempt {
            eprintln!("qmk-herdr: {error}; reconnecting");
            thread::sleep(Duration::from_secs(1));
        }
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("qmk-herdr: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent(id: &str, status: Status, seq: u64) -> Agent {
        Agent {
            pane_id: id.into(),
            agent_status: status,
            state_change_seq: seq,
        }
    }

    #[test]
    fn keeps_slots_stable_and_coalesces_transitions() {
        let mut tracker = Tracker::new();
        let first = tracker.update(
            vec![
                agent("p2", Status::Working, 2),
                agent("p1", Status::Idle, 1),
            ],
            false,
        );
        assert_eq!(first.slots, [0, 1, 7, 7]);
        assert!(!first.chime_done);

        let second = tracker.update(
            vec![
                agent("p2", Status::Done, 3),
                agent("p1", Status::Blocked, 4),
            ],
            true,
        );
        assert_eq!(second.slots, [2, 3, 7, 7]);
        assert_eq!(second.aggregate, Status::Blocked);
        assert!(second.chime_done && second.chime_blocked);

        let overflow = tracker.update(
            vec![
                agent("p1", Status::Idle, 1),
                agent("p2", Status::Idle, 2),
                agent("p3", Status::Idle, 3),
                agent("p4", Status::Idle, 4),
                agent("p5", Status::Idle, 5),
            ],
            true,
        );
        assert!(overflow.overflow);
        assert_eq!(overflow.slots, [0, 0, 0, 0]);

        let request = subscription_request(&[agent("w1:p1", Status::Idle, 1)]);
        let subscriptions = request["params"]["subscriptions"].as_array().unwrap();
        assert_eq!(subscriptions.len(), 4);
        assert_eq!(subscriptions[3]["pane_id"], "w1:p1");
    }
}
