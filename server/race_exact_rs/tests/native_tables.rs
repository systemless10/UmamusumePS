use race_exact_rs::{netsort,simulator::post_number};
#[test]
fn native_post_number_arrays() {
    assert_eq!((0..9).map(|i|post_number(i,9)).collect::<Vec<_>>(),[1,2,3,4,5,6,7,8,8]);
    assert_eq!((0..18).map(|i|post_number(i,18)).collect::<Vec<_>>(),[1,1,2,2,3,3,4,4,5,5,6,6,7,7,7,8,8,8]);
}
#[test]
fn introsort_equal_keys_matches_dotnet_reference_tie_order() {
    let mut a=(0..18).collect::<Vec<_>>();
    netsort::sort(&mut a,&|_,_|0);
    assert_eq!(a,[0,15,14,13,12,11,10,9,8,7,6,5,4,3,2,1,16,17]);
}
