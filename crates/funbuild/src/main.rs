fn main() -> std::process::ExitCode {
    std::process::ExitCode::from(funbuild::run(std::env::args_os()) as u8)
}
