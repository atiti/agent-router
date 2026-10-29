use std::collections::BTreeMap;
use zellij_tile::prelude::*;

const PIPE_NAME: &str = "agentroute-move-pane";

#[derive(Default)]
struct PaneMover {
    permission_granted: bool,
    permission_pending: bool,
    pending_move: Option<(u32, usize)>,
    status: String,
}

impl PaneMover {
    fn execute_pending_move(&mut self) -> bool {
        let Some((pane_id, tab_id)) = self.pending_move else {
            return false;
        };

        match break_panes_to_tab_with_id(&[PaneId::Terminal(pane_id)], tab_id, true) {
            Some(_) => {
                self.pending_move = None;
                self.status = format!("Moved terminal_{pane_id} to tab {tab_id}.");
                hide_self();
            }
            None => {
                self.status = format!("Zellij could not move terminal_{pane_id} to tab {tab_id}.");
            }
        }
        true
    }

    fn request_move_permission(&mut self) {
        self.permission_pending = true;
        self.status = "Waiting for Zellij's Change application state permission.".to_string();
        request_permission(&[PermissionType::ChangeApplicationState]);
    }
}

register_plugin!(PaneMover);

impl ZellijPlugin for PaneMover {
    fn load(&mut self, _configuration: BTreeMap<String, String>) {
        subscribe(&[EventType::PermissionRequestResult]);
        self.request_move_permission();
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(PermissionStatus::Granted) => {
                self.permission_pending = false;
                self.permission_granted = true;
                self.status = "Permission granted.".to_string();
                self.execute_pending_move()
            }
            Event::PermissionRequestResult(PermissionStatus::Denied) => {
                self.permission_pending = false;
                self.permission_granted = false;
                self.status = "Permission denied. Run `ar move` again to retry.".to_string();
                true
            }
            _ => false,
        }
    }

    fn pipe(&mut self, message: PipeMessage) -> bool {
        if !message.is_private || message.name != PIPE_NAME {
            return false;
        }

        let Some(payload) = message.payload else {
            return false;
        };
        match parse_move_request(&payload) {
            Ok(request) => self.pending_move = Some(request),
            Err(error) => {
                self.status = error.to_string();
                return true;
            }
        }

        if self.permission_granted {
            return self.execute_pending_move();
        }
        if !self.permission_pending {
            self.request_move_permission();
        }
        true
    }

    fn render(&mut self, _rows: usize, _cols: usize) {
        println!("AgentRoute pane mover");
        println!("{}", self.status);
    }
}

fn parse_move_request(payload: &str) -> Result<(u32, usize), &'static str> {
    let mut parts = payload.split_whitespace();
    let pane_id = parts
        .next()
        .and_then(|value| value.parse().ok())
        .ok_or("Expected a terminal pane ID and destination tab ID.")?;
    let tab_id = parts
        .next()
        .and_then(|value| value.parse().ok())
        .ok_or("Expected a terminal pane ID and destination tab ID.")?;
    if parts.next().is_some() {
        return Err("Expected exactly two IDs for the pane move.");
    }
    Ok((pane_id, tab_id))
}

#[cfg(test)]
mod tests {
    use super::parse_move_request;

    #[test]
    fn parses_terminal_and_tab_ids() {
        assert_eq!(parse_move_request("17 42"), Ok((17, 42)));
    }

    #[test]
    fn rejects_missing_or_extra_ids() {
        assert!(parse_move_request("17").is_err());
        assert!(parse_move_request("17 42 unexpected").is_err());
    }

    #[test]
    fn rejects_non_numeric_ids() {
        assert!(parse_move_request("plugin_17 42").is_err());
        assert!(parse_move_request("17 private").is_err());
    }
}
