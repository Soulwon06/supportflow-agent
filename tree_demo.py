from numpy.f2py.crackfortran import n
from collections import deque

class TreeNode:
    def __init__(self, value):
        self.value = value
        self.left = None
        self.right = None

root = TreeNode("A")

root.left = TreeNode("B")
root.right = TreeNode("C")

root.left.left = TreeNode("D")
root.left.right = TreeNode("E")

def dfs(node):
    if node is None:
        return
    print(node.value,end="")

    dfs(node.left)
    dfs(node.right)

def bfs(root):
    if root is None:
        return
    quene=deque([root])
    while quene:

        node=quene.popleft()

        print(node.value,end="")

        if node.left:
         quene.append(node.left)

        if node.right:
         quene.append(node.right)   

print("DFS:")
dfs(root)

print("\n")

print("BFS:")
bfs(root)

print()
        
