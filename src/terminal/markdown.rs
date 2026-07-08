use crossterm::style::Stylize;

pub fn print_markdown(text: &str) {
    let mut in_code = false;
    for line in text.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") {
            in_code = !in_code;
            println!("{}", line.dark_grey());
            continue;
        }
        if in_code {
            println!("{}", line.cyan());
        } else if trimmed.starts_with('#') {
            println!("{}", line.bold());
        } else if trimmed.starts_with("- ") || trimmed.starts_with("* ") {
            println!("{}", line.green());
        } else {
            println!("{line}");
        }
    }
}
