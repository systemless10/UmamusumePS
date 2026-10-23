use race_exact_rs::{data::{Assets,Master,RaceIn},simulator::Simulator};
use std::path::PathBuf;
use std::io::{BufRead,Write};
fn run()->Result<(),String> {
    let mut args=std::env::args().skip(1);
    let command=args.next().ok_or("usage: race_exact_rs simulate|init --input FILE|--input-dir DIR --master FILE --assets FILE --output FILE|DIR; serve --master FILE --assets FILE")?;
    if command!="simulate" && command!="init" && command!="serve" {return Err(format!("unknown command {command}"));}
    let mut input=None;let mut input_dir=None;let mut master=None;let mut assets=None;let mut output=None;let mut trace=false;let mut require=false;let mut max_ticks=20000;
    while let Some(arg)=args.next() {
        match arg.as_str() {
            "--input"=>input=args.next(),"--input-dir"=>input_dir=args.next(),
            "--master"=>master=args.next(),"--assets"=>assets=args.next(),"--output"=>output=args.next(),
            "--trace"=>trace=true,"--require-exact"=>require=true,
            "--max-ticks"=>max_ticks=args.next().ok_or("missing max ticks")?.parse().map_err(|_|"invalid max ticks")?,
            _=>return Err(format!("unknown argument {arg}")),
        }
    }
    if require {return Err("exact parity is not established; unresolved behavior remains (run without --require-exact to generate a diagnostic candidate)".into());}
    if command!="serve" && input.is_some()==input_dir.is_some() {return Err("supply exactly one of --input or --input-dir".into());}
    let master=Master::load(&master.ok_or("--master required")?)?;
    let assets=Assets::load(&assets.ok_or("--assets required")?)?;
    if command=="serve" {
        let mut stdout=std::io::BufWriter::new(std::io::stdout().lock());
        for line in std::io::stdin().lock().lines() {
            let line=line.map_err(|e|e.to_string())?;
            let result=serde_json::from_str::<RaceIn>(&line).map_err(|e|e.to_string())
                .and_then(|input|Simulator::new(input,&master,&assets,false))
                .and_then(|sim|sim.run(max_ticks));
            let value=result.unwrap_or_else(|error|serde_json::json!({"error":error}));
            serde_json::to_writer(&mut stdout,&value).map_err(|e|e.to_string())?;
            stdout.write_all(b"\n").map_err(|e|e.to_string())?;
            stdout.flush().map_err(|e|e.to_string())?;
        }
        return Ok(());
    }
    let output=PathBuf::from(output.ok_or("--output required")?);
    let single=|path:&str|->Result<serde_json::Value,String> {
        let sim=Simulator::new(RaceIn::load(path)?,&master,&assets,trace)?;
        if command=="init" {Ok(sim.initialization())} else {sim.run(max_ticks)}
    };
    let write=|path:&std::path::Path,value:&serde_json::Value|->Result<(),String> {
        let mut bytes=serde_json::to_vec(value).map_err(|e|e.to_string())?;bytes.push(b'\n');
        std::fs::write(path,bytes).map_err(|e|format!("{}: {e}",path.display()))
    };
    if let Some(directory)=input_dir {
        std::fs::create_dir_all(&output).map_err(|e|e.to_string())?;
        let mut paths=std::fs::read_dir(directory).map_err(|e|e.to_string())?
            .map(|e|e.map(|e|e.path())).collect::<Result<Vec<_>,_>>().map_err(|e|e.to_string())?;
        paths.retain(|p|p.is_file() && p.extension().is_some_and(|e|e=="json"));paths.sort();
        if paths.is_empty() {return Err("input directory contains no JSON races".into());}
        let next=std::sync::atomic::AtomicUsize::new(0);
        let threads=std::thread::available_parallelism().map_or(4,|n|n.get());
        let errors=std::sync::Mutex::new(Vec::new());
        std::thread::scope(|s|for _ in 0..threads {s.spawn(||loop {
            let i=next.fetch_add(1,std::sync::atomic::Ordering::Relaxed);
            let Some(path)=paths.get(i) else {break};
            let result=path.to_str().ok_or("non-UTF8 input path".to_string())
                .and_then(|p|single(p)).and_then(|v|write(&output.join(path.file_name().unwrap()),&v));
            if let Err(e)=result {errors.lock().unwrap().push(format!("{}: {e}",path.display()));}
        });});
        if let Some(e)=errors.into_inner().unwrap().into_iter().next() {return Err(e);}
        eprintln!("wrote {} {command} candidates",paths.len());
    } else {write(&output,&single(&input.unwrap())?)?;}
    Ok(())
}
fn main() {
    if let Err(e)=run() {eprintln!("race_exact_rs: {e}");std::process::exit(2);}
}
