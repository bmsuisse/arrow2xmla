//! A pivot answer from columnar data: `cargo run -p arrow2xmla --example basic`.

use arrow2xmla::{Array, Axis, Cellset, Column, LevelAxis, Member, MemberProp, MemberType, Tuples, Values};

fn main() -> Result<(), arrow2xmla::Error> {
    // the rows: one member per customer, read from a text column (Arrow layout: offsets + bytes)
    let names = ["Acme", "Globex", "Initech & Co"];
    let mut offsets = vec![0i32];
    let mut data = vec![];
    for n in names {
        data.extend_from_slice(n.as_bytes());
        offsets.push(data.len() as i32);
    }
    let mut customer = Member::new("[Customer].[Name]", "[Customer].[Name].[Name]", 1);
    customer.parent_unique_name = "[Customer].[Name].[All]".into();
    let mut rows = Axis::level(LevelAxis::new(customer, Array::new(Values::Utf8 { offsets: &offsets, data: &data })));
    rows.properties = vec![MemberProp::MemberCaption];

    // the columns: one measure
    let mut revenue = Member::new("[Measures]", "[Measures].[MeasuresLevel]", 0);
    revenue.unique_name = "[Measures].[Revenue]".into();
    revenue.caption = "Revenue".into();
    revenue.member_type = MemberType::Measure;
    let columns = Axis::tuples(Tuples::new(vec![revenue], vec![vec![0]]));

    let amounts = [1500.5, 920.0, 77.25];
    let mut cellset = Cellset::new("Sales");
    cellset.axes = vec![columns, rows];
    cellset.columns.push(Column::new(Array::new(Values::F64(&amounts)), "#,##0.00"));

    let xml = cellset.to_vec()?; // or cellset.write_to(&mut writer), or write_chunks for a stream
    println!("{} bytes of XMLA", xml.len());
    Ok(())
}
