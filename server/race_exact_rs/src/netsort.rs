pub fn sort<T: Clone>(a: &mut [T], cmp: &impl Fn(&T,&T)->i32) {
    if a.len()<2 {return;}
    let depth=2*(usize::BITS-a.len().leading_zeros()) as usize;
    intro(a,0,a.len()-1,depth,cmp);
}
fn swap_greater<T>(a:&mut[T],i:usize,j:usize,c:&impl Fn(&T,&T)->i32) {
    if i!=j && c(&a[i],&a[j])>0 {a.swap(i,j);}
}
fn down<T:Clone>(a:&mut[T],mut i:usize,n:usize,lo:usize,c:&impl Fn(&T,&T)->i32) {
    let d=a[lo+i-1].clone();
    while i<=n/2 {
        let mut child=2*i;
        if child<n && c(&a[lo+child-1],&a[lo+child])<0 {child+=1;}
        if c(&d,&a[lo+child-1])>=0 {break;}
        a[lo+i-1]=a[lo+child-1].clone();i=child;
    } a[lo+i-1]=d;
}
fn intro<T:Clone>(a:&mut[T],lo:usize,mut hi:usize,mut depth:usize,c:&impl Fn(&T,&T)->i32) {
    while hi>lo {
        let size=hi-lo+1;
        if size<=16 {
            match size {
                2=>swap_greater(a,lo,hi,c),
                3=>{swap_greater(a,lo,hi-1,c);swap_greater(a,lo,hi,c);swap_greater(a,hi-1,hi,c);},
                _=>for i in lo..hi {
                    let t=a[i+1].clone();let mut j=i+1;
                    while j>lo && c(&t,&a[j-1])<0 {a[j]=a[j-1].clone();j-=1;}
                    a[j]=t;
                }
            } return;
        }
        if depth==0 {
            let n=hi-lo+1;
            for i in (1..=n/2).rev() {down(a,i,n,lo,c);}
            for i in (2..=n).rev() {a.swap(lo,lo+i-1);down(a,1,i-1,lo,c);}
            return;
        }
        depth-=1;
        let mid=lo+((hi-lo)>>1);
        swap_greater(a,lo,mid,c);swap_greater(a,lo,hi,c);swap_greater(a,mid,hi,c);
        let pivot=a[mid].clone();a.swap(mid,hi-1);
        let mut left=lo;let mut right=hi-1;
        while left<right {
            left+=1;while c(&a[left],&pivot)<0 {left+=1;}
            right-=1;while c(&pivot,&a[right])<0 {right-=1;}
            if left>=right {break;}a.swap(left,right);
        }
        a.swap(left,hi-1);intro(a,left+1,hi,depth,c);hi=left-1;
    }
}
