import heapq

def top_k_frequent(nums,k):
    count={}
    for num in nums:
        count[num]=count.get(num,0)+1
        heap=[]
        for num,frequency in count.items():
            heapq.heappush(heap,(frequency,num))

            if len(heap)>k:
                heapq.heappop(heap)

    return [num for frequency, num in heap]

nums = [1, 1, 1, 2, 2, 3]
k = 2

print(top_k_frequent(nums, k))